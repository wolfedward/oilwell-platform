"""Catalogue loading must preserve routes, reject invalid entries and map annual tasks."""
import copy
import json
from collections import Counter
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'frontend/preview/catalog.json'


class PreviewCatalogTests(unittest.TestCase):
    def test_all_approved_pages_can_be_loaded_and_resolved(self):
        from backend.preview.catalog import load_catalog
        pages = load_catalog(CATALOG)
        spec = (ROOT / 'docs/superpowers/specs/2026-10-12-page-first-platform-design.md').read_text(encoding='utf-8')
        expected = set(re.findall(r'^\| [^|]+ \| `([a-z-]+)` \|', spec, re.M))
        self.assertEqual({p['id'] for p in pages}, expected)
        self.assertEqual(len(pages), 55)
        self.assertEqual(Counter(p['origin'] for p in pages), {'demo': 24, 'development': 11, 'new': 20})
        self.assertEqual({x for p in pages for x in p['annual_tasks']},
                         {'Y1-01','Y1-02','Y1-03','Y1-04','Y2-01','Y2-02','Y3-01','Y3-02','Y3-03','Y3-04','Y3-05','Y3-06'})
        self.assertEqual(len({p['group'] for p in pages}), 9)

    def test_duplicate_unknown_origin_missing_version_and_bad_route_rejected(self):
        from backend.preview.catalog import load_catalog, validate_catalog
        pages = load_catalog(CATALOG)
        for field, value in [('id', '../private'), ('origin', 'live'), ('content_version', ''), ('annual_tasks', ['Y4-01'])]:
            bad = copy.deepcopy(pages)
            bad[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_catalog(bad)
        with self.assertRaises(ValueError):
            validate_catalog(pages + [pages[0]])

    def test_browser_loader_uses_same_validated_catalog(self):
        script = """
          import {loadCatalog} from './frontend/preview/catalog.js';
          import {readFileSync} from 'node:fs';
          const pages = JSON.parse(readFileSync('./frontend/preview/catalog.json'));
          const loaded = await loadCatalog('/frontend/preview/catalog.json', async () => ({ok:true,json:async()=>pages}));
          let rejected = false;
          try { await loadCatalog('x', async()=>({ok:true,json:async()=>[...pages,pages[0]]})); } catch { rejected=true; }
          console.log(JSON.stringify({ids:loaded.map(p=>p.id),rejected}));
        """
        result = subprocess.run(['node','--input-type=module','--eval',script],cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(len(set(data['ids'])), 55)
        self.assertTrue(data['rejected'])
