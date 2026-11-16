"""R-E-1 (second review): `notify_command` may not name a launcher, mailer or build tool that runs its arguments. The first round's list had `env`, `xargs` and the shells;
`arch sh /tmp/x.sh`, `xcrun sh`, `caffeinate sh`, `script /dev/null sh` and `mail -s x a@b` still got through. A denylist is a typo guard: the real stop is that the
program must be one the human named, outside the repository, with plain arguments (see notify.py)."""
from __future__ import annotations

import unittest

from guards import paths  # noqa: F401  (puts the scripts directory on sys.path)

import notify


class DenyList(unittest.TestCase):
    def test_the_reviewers_launchers_and_the_ones_next_to_them_are_refused(self):
        for prog in ("arch", "xcrun", "caffeinate", "script", "mail", "watch", "flock", "taskset", "ionice", "chroot", "strace", "parallel",
                     "ltrace", "mailx", "sendmail", "unbuffer", "sandbox-exec", "systemd-run", "git", "make", "npm", "docker", "tmux", "vim", "less", "tar",
                     "/usr/bin/arch", "/usr/bin/xcrun", "/usr/bin/script", "/usr/bin/mail", "/usr/bin/flock", "/opt/homebrew/bin/parallel"):
            with self.subTest(prog=prog):
                self.assertTrue(notify._denied_program(prog), prog)

    def test_a_program_the_human_wrote_is_still_accepted(self):
        for prog in ("ntfy", "/usr/local/bin/notify-me", "terminal-notifier", "/home/me/bin/ping-phone", "notify-send", "say"):
            with self.subTest(prog=prog):
                self.assertFalse(notify._denied_program(prog), prog)


if __name__ == "__main__":
    unittest.main()
