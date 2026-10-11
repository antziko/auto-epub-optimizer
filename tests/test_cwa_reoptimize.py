import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'cwa-reoptimize.py'
spec = importlib.util.spec_from_file_location('cwa_reoptimize', SCRIPT)
cwa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cwa)


def book(id_, title, authors='Lee Kuan Yew', formats=('/calibre-library/a/b.epub',)):
    return {'id': id_, 'title': title, 'authors': authors, 'formats': list(formats)}


class ClassifyTests(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(cwa.classify('(X4) Wit'), ('legacy', 'Wit'))
        self.assertEqual(cwa.classify('Wit (X3)'), ('legacy', 'Wit'))
        self.assertEqual(cwa.classify('(x) Wit'), ('optimized', 'Wit'))
        self.assertEqual(cwa.classify('Wit'), ('original', 'Wit'))
        self.assertEqual(cwa.classify('Mac OS X (X11)'), ('original', 'Mac OS X (X11)'))


class PlanTests(unittest.TestCase):
    def test_plan(self):
        books = [
            book(11, 'The Wit & Wisdom'),
            book(68, '(X3) The Wit & Wisdom'),
            book(69, '(X4) The Wit & Wisdom'),
            book(70, '(X4) Lost Original'),            # no untagged original -> kept
            book(132, 'From Third World to First'),
            book(133, '(x) From Third World to First'),  # already optimized
            book(16, 'The Singapore Story'),
            book(20, 'Done Earlier'),                   # in state file
            book(21, 'Only PDF', formats=['/calibre-library/c/d.pdf']),
        ]
        remove, orphans, jobs, no_epub, skipped = cwa.plan(books, done_ids={20})
        self.assertEqual(sorted(b['id'] for b in remove), [68, 69])
        self.assertEqual([b['id'] for b in orphans], [70])
        self.assertEqual([b['id'] for b in jobs], [11, 16])
        self.assertEqual([b['id'] for b in no_epub], [21])
        self.assertEqual(skipped, 2)

    def test_match_ignores_case_and_spacing_but_not_author(self):
        books = [book(1, 'Dune', 'Frank Herbert'), book(2, '(X4)  dune', 'frank herbert'),
                 book(3, '(X3) Dune', 'Someone Else')]
        remove, orphans, _, _, _ = cwa.plan(books, set())
        self.assertEqual([b['id'] for b in remove], [2])
        self.assertEqual([b['id'] for b in orphans], [3])


if __name__ == '__main__':
    unittest.main()
