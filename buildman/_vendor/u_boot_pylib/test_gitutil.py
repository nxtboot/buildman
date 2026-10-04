# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>

"""Tests for the cwd-style helpers in u_boot_pylib.gitutil"""

import os
import shutil
import subprocess
import tempfile
import unittest

from u_boot_pylib import gitutil


def _git(*args, cwd):
    """Run git silently in cwd, raising on failure."""
    subprocess.run(['git', *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _commit(cwd, fname, body, msg):
    """Add a tracked file and commit it."""
    with open(os.path.join(cwd, fname), 'w') as f:
        f.write(body)
    _git('add', fname, cwd=cwd)
    _git('-c', 'user.email=t@e', '-c', 'user.name=T',
         'commit', '-m', msg, cwd=cwd)


def _rev(cwd, ref='HEAD'):
    """Return the full hash which a ref resolves to"""
    return subprocess.run(['git', 'rev-parse', ref], cwd=cwd, check=True,
                          stdout=subprocess.PIPE,
                          text=True).stdout.strip()


class TestGitutilHelpers(unittest.TestCase):
    """Tests for the gitutil helpers introduced for review.py."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='gitutil-test-')
        _git('init', '-q', '-b', 'main', cwd=self.tmpdir)
        _commit(self.tmpdir, 'a', 'first', 'first')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_count_revs_empty_range(self):
        """count_revs() returns 0 when the range contains no commits."""
        self.assertEqual(0, gitutil.count_revs(self.tmpdir, 'main..main'))

    def test_count_revs_with_commits(self):
        """count_revs() returns the number of commits in the range."""
        _git('checkout', '-q', '-b', 'work', cwd=self.tmpdir)
        _commit(self.tmpdir, 'b', 'second', 'second')
        _commit(self.tmpdir, 'c', 'third', 'third')
        self.assertEqual(2, gitutil.count_revs(self.tmpdir, 'main..work'))

    def test_count_revs_invalid_range(self):
        """count_revs() returns None when one side of the range is bad."""
        self.assertIsNone(
            gitutil.count_revs(self.tmpdir, 'no-such-ref..main'))

    def test_diff_stat(self):
        """diff_stat() reports a stat summary for a valid range."""
        _git('checkout', '-q', '-b', 'work', cwd=self.tmpdir)
        _commit(self.tmpdir, 'b', 'second', 'second')
        out = gitutil.diff_stat('main..work', self.tmpdir)
        self.assertIn('b', out)
        self.assertIn('insert', out)

    def test_diff_stat_invalid(self):
        """diff_stat() returns '' when the range is invalid."""
        self.assertEqual(
            '', gitutil.diff_stat('no-such-ref..main', self.tmpdir))

    def test_ref_exists(self):
        """ref_exists() resolves valid refs and rejects missing ones."""
        self.assertTrue(gitutil.ref_exists('main', self.tmpdir))
        self.assertFalse(gitutil.ref_exists('does-not-exist', self.tmpdir))

    def test_current_branch(self):
        """current_branch() returns the active branch name."""
        self.assertEqual('main', gitutil.current_branch(self.tmpdir))
        _git('checkout', '-q', '-b', 'feature', cwd=self.tmpdir)
        self.assertEqual('feature', gitutil.current_branch(self.tmpdir))

    def test_checkout_branch(self):
        """checkout_branch() swaps the active branch."""
        _git('branch', 'other', cwd=self.tmpdir)
        gitutil.checkout_branch('other', self.tmpdir)
        self.assertEqual('other', gitutil.current_branch(self.tmpdir))

    def test_stash_save_pop(self):
        """stash_save() saves a tracked-file change; stash_pop() restores it."""
        with open(os.path.join(self.tmpdir, 'a'), 'w') as f:
            f.write('changed')
        out = gitutil.stash_save(self.tmpdir)
        self.assertNotIn('No local changes', out)
        with open(os.path.join(self.tmpdir, 'a')) as f:
            self.assertEqual('first', f.read())
        gitutil.stash_pop(self.tmpdir)
        with open(os.path.join(self.tmpdir, 'a')) as f:
            self.assertEqual('changed', f.read())

    def test_stash_save_clean_tree(self):
        """stash_save() reports no changes when nothing is modified."""
        out = gitutil.stash_save(self.tmpdir)
        self.assertIn('No local changes', out)

    def test_stash_save_include_untracked(self):
        """stash_save(include_untracked=True) covers untracked files too."""
        path = os.path.join(self.tmpdir, 'extra')
        with open(path, 'w') as f:
            f.write('untracked')

        # Default: untracked files are not stashed.
        out = gitutil.stash_save(self.tmpdir)
        self.assertIn('No local changes', out)
        self.assertTrue(os.path.exists(path))

        # With include_untracked, the file is stashed away.
        out = gitutil.stash_save(self.tmpdir, include_untracked=True)
        self.assertNotIn('No local changes', out)
        self.assertFalse(os.path.exists(path))


class TestGitutilDrift(unittest.TestCase):
    """Tests for the gitutil helpers introduced for pickman drift"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='gitutil-test-')
        _git('init', '-q', '-b', 'main', cwd=self.tmpdir)
        # A persistent identity, so the commit-creating helpers work
        _git('config', 'user.email', 't@e', cwd=self.tmpdir)
        _git('config', 'user.name', 'T', cwd=self.tmpdir)
        _commit(self.tmpdir, 'a', 'first', 'first')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read(self, fname):
        """Return the contents of a file in the test repo"""
        with open(os.path.join(self.tmpdir, fname)) as f:
            return f.read()

    def test_count_revs_first_parent_and_merges(self):
        """count_revs() can restrict to the first parent or to merges"""
        base = _rev(self.tmpdir)
        _git('checkout', '-q', '-b', 'work', cwd=self.tmpdir)
        _commit(self.tmpdir, 'b', 'second', 'b')
        _commit(self.tmpdir, 'c', 'third', 'c')
        _git('checkout', '-q', 'main', cwd=self.tmpdir)
        _git('-c', 'user.email=t@e', '-c', 'user.name=T', 'merge', '--no-ff',
             '-q', '-m', 'merge work', 'work', cwd=self.tmpdir)
        rng = f'{base}..main'
        # b, c and the merge
        self.assertEqual(3, gitutil.count_revs(self.tmpdir, rng))
        # Only the merge
        self.assertEqual(
            1, gitutil.count_revs(self.tmpdir, rng, merges=True))
        # b and c, but not the merge
        self.assertEqual(
            2, gitutil.count_revs(self.tmpdir, rng, merges=False))
        # Only the merge sits on the first-parent chain
        self.assertEqual(
            1, gitutil.count_revs(self.tmpdir, rng, first_parent=True))

    def test_merge_base(self):
        """merge_base() finds the common ancestor of two commits"""
        base = _rev(self.tmpdir)
        _git('checkout', '-q', '-b', 'work', cwd=self.tmpdir)
        _commit(self.tmpdir, 'b', 'second', 'b')
        _git('checkout', '-q', 'main', cwd=self.tmpdir)
        _commit(self.tmpdir, 'c', 'third', 'c')
        self.assertEqual(
            base, gitutil.merge_base('work', 'main', git_dir=self.tmpdir))

    def test_diff(self):
        """diff() returns the patch between two commits"""
        _git('checkout', '-q', '-b', 'work', cwd=self.tmpdir)
        _commit(self.tmpdir, 'b', 'second', 'add b')
        out = gitutil.diff('main', 'work', git_dir=self.tmpdir)
        self.assertIn('diff --git a/b b/b', out)
        self.assertIn('+second', out)

    def test_blame(self):
        """blame() returns porcelain blame for a file"""
        out = gitutil.blame('main', 'a', git_dir=self.tmpdir)
        self.assertIn('author T', out)
        self.assertIn('\tfirst', out)

    def test_log_hashes(self):
        """log_hashes() lists hashes in a range, honouring grep/no_merges"""
        _git('checkout', '-q', '-b', 'work', cwd=self.tmpdir)
        _commit(self.tmpdir, 'b', 'second', 'plain')
        want = _rev(self.tmpdir)
        _commit(self.tmpdir, 'c', 'third', 'special marker')
        marker = _rev(self.tmpdir)
        self.assertEqual(
            [marker, want], gitutil.log_hashes('main..work',
                                               git_dir=self.tmpdir))
        self.assertEqual(
            [marker], gitutil.log_hashes('main..work', grep='marker',
                                         git_dir=self.tmpdir))

    def test_log_commits_with_files(self):
        """log_commits_with_files() pairs each commit with its files"""
        _git('checkout', '-q', '-b', 'work', cwd=self.tmpdir)
        _commit(self.tmpdir, 'b', 'second', 'b')
        _commit(self.tmpdir, 'c', 'third', 'c')
        commits = gitutil.log_commits_with_files('main..work',
                                                 git_dir=self.tmpdir)
        # Newest first
        self.assertEqual([files for _, files in commits], [['c'], ['b']])

    def test_commit_summary(self):
        """commit_summary() returns the abbreviated hash and subject"""
        out = gitutil.commit_summary('main', git_dir=self.tmpdir)
        self.assertTrue(out.endswith(' first'), out)

    def test_branch_exists(self):
        """branch_exists() reports whether a local branch exists"""
        self.assertTrue(gitutil.branch_exists('main', git_dir=self.tmpdir))
        self.assertFalse(gitutil.branch_exists('nope', git_dir=self.tmpdir))

    def test_delete_branch(self):
        """delete_branch() removes a branch even if not merged"""
        _git('branch', 'tmp', cwd=self.tmpdir)
        self.assertTrue(gitutil.branch_exists('tmp', git_dir=self.tmpdir))
        gitutil.delete_branch('tmp', git_dir=self.tmpdir)
        self.assertFalse(gitutil.branch_exists('tmp', git_dir=self.tmpdir))

    def test_create_branch(self):
        """create_branch() creates a branch and checks it out"""
        gitutil.create_branch('feat', git_dir=self.tmpdir)
        self.assertEqual('feat', gitutil.current_branch(self.tmpdir))
        self.assertTrue(gitutil.branch_exists('feat', git_dir=self.tmpdir))

    def test_apply_patch(self):
        """apply_patch() applies a patch and reverses it with reverse=True"""
        _commit(self.tmpdir, 'a', 'second', 'change a')
        patch = gitutil.diff('HEAD~1', 'HEAD', git_dir=self.tmpdir)
        _git('reset', '--hard', '-q', 'HEAD~1', cwd=self.tmpdir)
        with open(os.path.join(self.tmpdir, 'p.patch'), 'w') as f:
            f.write(patch)
        gitutil.apply_patch('p.patch', git_dir=self.tmpdir)
        self.assertEqual('second', self._read('a'))
        gitutil.apply_patch('p.patch', reverse=True, whitespace='nowarn',
                            git_dir=self.tmpdir)
        self.assertEqual('first', self._read('a'))

    def test_checkout_paths(self):
        """checkout_paths() restores paths from a commit"""
        with open(os.path.join(self.tmpdir, 'a'), 'w') as f:
            f.write('changed')
        gitutil.checkout_paths('HEAD', ['a'], git_dir=self.tmpdir)
        self.assertEqual('first', self._read('a'))

    def test_add_and_commit_paths(self):
        """add() stages files and commit_paths() commits them"""
        with open(os.path.join(self.tmpdir, 'd'), 'w') as f:
            f.write('data')
        gitutil.add(['d'], git_dir=self.tmpdir)
        gitutil.commit_paths('add d', ['d'], git_dir=self.tmpdir)
        self.assertTrue(
            gitutil.commit_summary('HEAD', git_dir=self.tmpdir).endswith(
                ' add d'))
        self.assertFalse(gitutil.has_uncommitted_changes(self.tmpdir))

    def test_has_uncommitted_changes(self):
        """has_uncommitted_changes() spots changes to tracked files"""
        self.assertFalse(gitutil.has_uncommitted_changes(self.tmpdir))
        with open(os.path.join(self.tmpdir, 'a'), 'w') as f:
            f.write('changed')
        self.assertTrue(gitutil.has_uncommitted_changes(self.tmpdir))


if __name__ == '__main__':
    unittest.main()
