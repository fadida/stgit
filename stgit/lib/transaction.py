"""The L{StackTransaction} class makes it possible to make complex
updates to an StGit stack in a safe and convenient way."""

import atexit
import itertools as it

from stgit import exception, utils
from stgit.utils import any, all
from stgit.out import *
from stgit.lib import git, log

class TransactionException(exception.StgException):
    """Exception raised when something goes wrong with a
    L{StackTransaction}."""

class TransactionHalted(TransactionException):
    """Exception raised when a L{StackTransaction} stops part-way through.
    Used to make a non-local jump from the transaction setup to the
    part of the transaction code where the transaction is run."""

def _print_current_patch(old_applied, new_applied):
    def now_at(pn):
        out.info('Now at patch "%s"' % pn)
    if not old_applied and not new_applied:
        pass
    elif not old_applied:
        now_at(new_applied[-1])
    elif not new_applied:
        out.info('No patch applied')
    elif old_applied[-1] == new_applied[-1]:
        pass
    else:
        now_at(new_applied[-1])

class _TransPatchMap(dict):
    """Maps patch names to sha1 strings."""
    def __init__(self, stack):
        dict.__init__(self)
        self.__stack = stack
    def __getitem__(self, pn):
        try:
            return dict.__getitem__(self, pn)
        except KeyError:
            return self.__stack.patches.get(pn).commit

class StackTransaction(object):
    """A stack transaction, used for making complex updates to an StGit
    stack in one single operation that will either succeed or fail
    cleanly.

    The basic theory of operation is the following:

      1. Create a transaction object.

      2. Inside a::

         try
           ...
         except TransactionHalted:
           pass

      block, update the transaction with e.g. methods like
      L{pop_patches} and L{push_patch}. This may create new git
      objects such as commits, but will not write any refs; this means
      that in case of a fatal error we can just walk away, no clean-up
      required.

      (Some operations may need to touch your index and working tree,
      though. But they are cleaned up when needed.)

      3. After the C{try} block -- wheher or not the setup ran to
      completion or halted part-way through by raising a
      L{TransactionHalted} exception -- call the transaction's L{run}
      method. This will either succeed in writing the updated state to
      your refs and index+worktree, or fail without having done
      anything."""
    def __init__(self, stack, msg, discard_changes = False,
                 allow_conflicts = False, allow_bad_head = False):
        """Create a new L{StackTransaction}.

        @param discard_changes: Discard any changes in index+worktree
        @type discard_changes: bool
        @param allow_conflicts: Whether to allow pre-existing conflicts
        @type allow_conflicts: bool or function of L{StackTransaction}"""
        self.__stack = stack
        self.__msg = msg
        self.__patches = _TransPatchMap(stack)
        self.__applied = list(self.__stack.patchorder.applied)
        self.__unapplied = list(self.__stack.patchorder.unapplied)
        self.__hidden = list(self.__stack.patchorder.hidden)
        self.__conflicting_push = None
        self.__error = None
        self.__current_tree = self.__stack.head.data.tree
        self.__base = self.__stack.base
        self.__discard_changes = discard_changes
        self.__bad_head = None
        if isinstance(allow_conflicts, bool):
            self.__allow_conflicts = lambda trans: allow_conflicts
        else:
            self.__allow_conflicts = allow_conflicts
        self.__temp_index = self.temp_index_tree = None
        if not allow_bad_head:
            self.__assert_head_top_equal()
    stack = property(lambda self: self.__stack)
    patches = property(lambda self: self.__patches)
    def __set_applied(self, val):
        self.__applied = list(val)
    applied = property(lambda self: self.__applied, __set_applied)
    def __set_unapplied(self, val):
        self.__unapplied = list(val)
    unapplied = property(lambda self: self.__unapplied, __set_unapplied)
    def __set_hidden(self, val):
        self.__hidden = list(val)
    hidden = property(lambda self: self.__hidden, __set_hidden)
    all_patches = property(lambda self: (self.__applied + self.__unapplied
                                         + self.__hidden))
    def __set_base(self, val):
        assert (not self.__applied
                or self.patches[self.applied[0]].data.parent == val)
        self.__base = val
    base = property(lambda self: self.__base, __set_base)
    @property
    def temp_index(self):
        if not self.__temp_index:
            self.__temp_index = self.__stack.repository.temp_index()
            atexit.register(self.__temp_index.delete)
        return self.__temp_index
    @property
    def top(self):
        if self.__applied:
            return self.__patches[self.__applied[-1]]
        else:
            return self.__base
    def __get_head(self):
        if self.__bad_head:
            return self.__bad_head
        else:
            return self.top
    def __set_head(self, val):
        self.__bad_head = val
    head = property(__get_head, __set_head)
    def __assert_head_top_equal(self):
        if not self.__stack.head_top_equal():
            out.error(
                'HEAD and top are not the same.',
                'This can happen if you modify a branch with git.',
                '"stg repair --help" explains more about what to do next.')
            self.__abort()
    def __checkout(self, tree, iw, allow_bad_head):
        if not allow_bad_head:
            self.__assert_head_top_equal()
        if self.__current_tree == tree and not self.__discard_changes:
            # No tree change, but we still want to make sure that
            # there are no unresolved conflicts. Conflicts
            # conceptually "belong" to the topmost patch, and just
            # carrying them along to another patch is confusing.
            if (self.__allow_conflicts(self) or iw == None
                or not iw.index.conflicts()):
                return
            out.error('Need to resolve conflicts first')
            self.__abort()
        assert iw != None
        if self.__discard_changes:
            iw.checkout_hard(tree)
        else:
            iw.checkout(self.__current_tree, tree)
        self.__current_tree = tree
    @staticmethod
    def __abort():
        raise TransactionException(
            'Command aborted (all changes rolled back)')
    def __check_consistency(self):
        remaining = set(self.all_patches)
        for pn, commit in self.__patches.iteritems():
            if commit == None:
                assert self.__stack.patches.exists(pn)
            else:
                assert pn in remaining
    def abort(self, iw = None):
        # The only state we need to restore is index+worktree.
        if iw:
            self.__checkout(self.__stack.head.data.tree, iw,
                            allow_bad_head = True)
    def run(self, iw = None, set_head = True, allow_bad_head = False,
            print_current_patch = True):
        """Execute the transaction. Will either succeed, or fail (with an
        exception) and do nothing."""
        self.__check_consistency()
        log.log_external_mods(self.__stack)
        new_head = self.head

        # Set branch head.
        if set_head:
            if iw:
                try:
                    self.__checkout(new_head.data.tree, iw, allow_bad_head)
                except git.CheckoutException:
                    # We have to abort the transaction.
                    self.abort(iw)
                    self.__abort()
            self.__stack.set_head(new_head, self.__msg)

        if self.__error:
            out.error(self.__error)

        # Write patches.
        def write(msg):
            for pn, commit in self.__patches.iteritems():
                if self.__stack.patches.exists(pn):
                    p = self.__stack.patches.get(pn)
                    if commit == None:
                        p.delete()
                    else:
                        p.set_commit(commit, msg)
                else:
                    self.__stack.patches.new(pn, commit, msg)
            self.__stack.patchorder.applied = self.__applied
            self.__stack.patchorder.unapplied = self.__unapplied
            self.__stack.patchorder.hidden = self.__hidden
            log.log_entry(self.__stack, msg)
        old_applied = self.__stack.patchorder.applied
        write(self.__msg)
        if self.__conflicting_push != None:
            self.__patches = _TransPatchMap(self.__stack)
            self.__conflicting_push()
            write(self.__msg + ' (CONFLICT)')
        if print_current_patch:
            _print_current_patch(old_applied, self.__applied)

        if self.__error:
            return utils.STGIT_CONFLICT
        else:
            return utils.STGIT_SUCCESS

    def __halt(self, msg):
        self.__error = msg
        raise TransactionHalted(msg)

    @staticmethod
    def __print_popped(popped):
        if len(popped) == 0:
            pass
        elif len(popped) == 1:
            out.info('Popped %s' % popped[0])
        else:
            out.info('Popped %s -- %s' % (popped[-1], popped[0]))

    def pop_patches(self, p):
        """Pop all patches pn for which p(pn) is true. Return the list of
        other patches that had to be popped to accomplish this. Always
        succeeds."""
        popped = []
        for i in xrange(len(self.applied)):
            if p(self.applied[i]):
                popped = self.applied[i:]
                del self.applied[i:]
                break
        popped1 = [pn for pn in popped if not p(pn)]
        popped2 = [pn for pn in popped if p(pn)]
        self.unapplied = popped1 + popped2 + self.unapplied
        self.__print_popped(popped)
        return popped1

    def delete_patches(self, p, quiet = False):
        """Delete all patches pn for which p(pn) is true. Return the list of
        other patches that had to be popped to accomplish this. Always
        succeeds."""
        popped = []
        all_patches = self.applied + self.unapplied + self.hidden
        for i in xrange(len(self.applied)):
            if p(self.applied[i]):
                popped = self.applied[i:]
                del self.applied[i:]
                break
        popped = [pn for pn in popped if not p(pn)]
        self.unapplied = popped + [pn for pn in self.unapplied if not p(pn)]
        self.hidden = [pn for pn in self.hidden if not p(pn)]
        self.__print_popped(popped)
        for pn in all_patches:
            if p(pn):
                s = ['', ' (empty)'][self.patches[pn].data.is_nochange()]
                self.patches[pn] = None
                if not quiet:
                    out.info('Deleted %s%s' % (pn, s))
        return popped

    def push_patch(self, pn, iw = None):
        """Attempt to push the named patch. If this results in conflicts,
        halts the transaction. If index+worktree are given, spill any
        conflicts to them."""
        orig_cd = self.patches[pn].data
        cd = orig_cd.set_committer(None)
        oldparent = cd.parent
        cd = cd.set_parent(self.top)
        base = oldparent.data.tree
        ours = cd.parent.data.tree
        theirs = cd.tree
        tree, self.temp_index_tree = self.temp_index.merge(
            base, ours, theirs, self.temp_index_tree)
        s = ''
        merge_conflict = False
        if not tree:
            if iw == None:
                self.__halt('%s does not apply cleanly' % pn)
            try:
                self.__checkout(ours, iw, allow_bad_head = False)
            except git.CheckoutException:
                self.__halt('Index/worktree dirty')
            try:
                iw.merge(base, ours, theirs)
                tree = iw.index.write_tree()
                self.__current_tree = tree
                s = ' (modified)'
            except git.MergeConflictException:
                tree = ours
                merge_conflict = True
                s = ' (conflict)'
            except git.MergeException, e:
                self.__halt(str(e))
        cd = cd.set_tree(tree)
        if any(getattr(cd, a) != getattr(orig_cd, a) for a in
               ['parent', 'tree', 'author', 'message']):
            comm = self.__stack.repository.commit(cd)
        else:
            comm = None
            s = ' (unmodified)'
        if not merge_conflict and cd.is_nochange():
            s = ' (empty)'
        out.info('Pushed %s%s' % (pn, s))
        def update():
            if comm:
                self.patches[pn] = comm
            if pn in self.hidden:
                x = self.hidden
            else:
                x = self.unapplied
            del x[x.index(pn)]
            self.applied.append(pn)
        if merge_conflict:
            # We've just caused conflicts, so we must allow them in
            # the final checkout.
            self.__allow_conflicts = lambda trans: True

            # Save this update so that we can run it a little later.
            self.__conflicting_push = update
            self.__halt('Merge conflict')
        else:
            # Update immediately.
            update()

    def reorder_patches(self, applied, unapplied, hidden, iw = None):
        """Push and pop patches to attain the given ordering."""
        common = len(list(it.takewhile(lambda (a, b): a == b,
                                       zip(self.applied, applied))))
        to_pop = set(self.applied[common:])
        self.pop_patches(lambda pn: pn in to_pop)
        for pn in applied[common:]:
            self.push_patch(pn, iw)
        assert self.applied == applied
        assert set(self.unapplied + self.hidden) == set(unapplied + hidden)
        self.unapplied = unapplied
        self.hidden = hidden
