import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'publish.py'
spec = importlib.util.spec_from_file_location('portable_publisher', SCRIPT)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def mkzip(path, text='hello'):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('readme.txt', text)


class PublisherTests(unittest.TestCase):
    def test_collect_zip_and_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base/'dist').mkdir()
            mkzip(base/'dist'/'Portable.zip')
            (base/'dist'/'not-zip.txt').write_text('ignore')
            files = publisher.collect_zips('dist/*.zip', base)
            self.assertEqual([p.name for p in files], ['Portable.zip'])
            self.assertEqual(len(publisher.checksums(files).split('  ')[0]), 64)

    def test_missing_zip_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'No finished ZIP'):
                publisher.collect_zips('dist/*.zip', Path(tmp))

    def test_escape_glob_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pat in ('../*.zip', '/tmp/*.zip', 'C:/temp/*.zip', '..\\*.zip'):
                with self.subTest(pat=pat), self.assertRaisesRegex(ValueError, 'Unsafe'):
                    publisher.collect_zips(pat, Path(tmp))

    def test_same_name_from_two_folders_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base/'a').mkdir(); (base/'b').mkdir()
            mkzip(base/'a'/'App.zip', 'a'); mkzip(base/'b'/'App.zip', 'b')
            with self.assertRaisesRegex(ValueError, 'same filename'):
                publisher.collect_zips('a/*.zip;b/*.zip', base)

    def test_invalid_zip_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base/'fake.zip').write_text('not a zip')
            with self.assertRaisesRegex(ValueError, 'Not a valid ZIP'):
                publisher.collect_zips('*.zip', base)

    def test_tag_must_have_prefix(self):
        publisher.validate_tag('portable-v1.0-r1', 'portable-')
        with self.assertRaisesRegex(ValueError, 'prefix'):
            publisher.validate_tag('v1.0', 'portable-')
        with self.assertRaisesRegex(ValueError, 'tag is required'):
            publisher.validate_tag('', 'portable-')

    def test_prune_protects_manual_immutable_and_current(self):
        releases = [
            dict(tagName='portable-v4', publishedAt='2026-04-01', isDraft=False, isImmutable=False),
            dict(tagName='portable-v3', publishedAt='2026-03-01', isDraft=False, isImmutable=False),
            dict(tagName='portable-v2', publishedAt='2026-02-01', isDraft=False, isImmutable=True),
            dict(tagName='portable-v1', publishedAt='2026-01-01', isDraft=False, isImmutable=False),
            dict(tagName='official-v1', publishedAt='2026-01-01', isDraft=False, isImmutable=False),
            dict(tagName='portable-draft', publishedAt='2026-05-01', isDraft=True, isImmutable=False),
            dict(tagName='portable-unmanaged', publishedAt='2026-01-01', isDraft=False, isImmutable=False)
        ]
        delete = publisher.choose_old_releases(
            releases, 'portable-', 'portable-v4', 2,
            lambda t: t not in {'portable-unmanaged', 'official-v1'})
        self.assertEqual(delete, ['portable-v1'])

    def test_prune_disabled_with_zero(self):
        self.assertEqual(publisher.choose_old_releases([], 'portable-', 'portable-v2', 0, lambda _: True), [])

    def test_bool_validate(self):
        with patch.dict(os.environ, {'PUBLISH_PRUNE':'yes'}):
            with self.assertRaisesRegex(ValueError, 'true'):
                publisher.bool_setting('PUBLISH_PRUNE')

    def test_dry_run_does_not_require_credentials_or_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mkzip(base/'Portable.zip')
            values = dict(PUBLISH_FILES='Portable.zip', PUBLISH_TAG='portable-v1.2.3-r1',
                          PUBLISH_DRY_RUN='true', PUBLISH_PRUNE='false', WORKSPACE_ROOT=tmp,
                          TARGET_REPO='', GH_TOKEN='')
            with patch.dict(os.environ, values, clear=True):
                self.assertEqual(publisher.main(), 0)

    def test_non_dry_run_rejects_pr_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            mkzip(Path(tmp)/'Portable.zip')
            values = dict(PUBLISH_FILES='Portable.zip', PUBLISH_TAG='portable-v1.2.3-r1',
                          PUBLISH_DRY_RUN='false', ACTION_EVENT='pull_request', WORKSPACE_ROOT=tmp)
            with patch.dict(os.environ, values, clear=True):
                with self.assertRaisesRegex(ValueError, 'disallowed'):
                    publisher.main()

    def test_existing_release_nonowned_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            mkzip(Path(tmp)/'Portable.zip')
            values = dict(PUBLISH_FILES='Portable.zip', PUBLISH_TAG='portable-v1.2.3-r1',
                PUBLISH_DRY_RUN='false', ACTION_EVENT='workflow_dispatch', WORKSPACE_ROOT=tmp,
                TARGET_REPO='vhick/test-builder', GH_TOKEN='pretend-token', SOURCE_SHA='abcd')
            with patch.dict(os.environ, values, clear=True), \
                 patch.object(publisher.shutil, 'which', return_value='/usr/bin/gh'), \
                 patch.object(publisher, 'view_release', return_value={'isDraft': False, 'body': 'Manual'}):
                with self.assertRaisesRegex(RuntimeError, 'not created'):
                    publisher.main()

    def test_identical_existing_release_skips_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / 'Portable.zip'
            mkzip(package)
            expected = publisher.checksums([package])
            values = dict(PUBLISH_FILES='Portable.zip', PUBLISH_TAG='portable-v1-r1',
                          PUBLISH_DRY_RUN='false', ACTION_EVENT='workflow_dispatch',
                          WORKSPACE_ROOT=tmp, TARGET_REPO='vhick/test-builder',
                          GH_TOKEN='pretend', SOURCE_SHA='abcd')
            owned = {'body': publisher.MARKER, 'isDraft': False, 'isImmutable': False,
                     'assets': [{'name': 'Portable.zip', 'size': package.stat().st_size},
                                {'name': 'SHA256SUMS.txt', 'size': len(expected.encode())}]}
            with patch.dict(os.environ, values, clear=True), \
                 patch.object(publisher.shutil, 'which', return_value='/usr/bin/gh'), \
                 patch.object(publisher, 'view_release', return_value=owned), \
                 patch.object(publisher, 'remote_assets_match', return_value=True), \
                 patch.object(publisher, 'run_gh') as gh:
                self.assertEqual(publisher.main(), 0)
                gh.assert_not_called()

    def test_changed_existing_release_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            mkzip(Path(tmp)/'Portable.zip')
            values = dict(PUBLISH_FILES='Portable.zip', PUBLISH_TAG='portable-v1-r1',
                          PUBLISH_DRY_RUN='false', ACTION_EVENT='workflow_dispatch',
                          WORKSPACE_ROOT=tmp, TARGET_REPO='vhick/test-builder',
                          GH_TOKEN='pretend', SOURCE_SHA='abcd')
            owned = {'body': publisher.MARKER, 'isDraft': False}
            with patch.dict(os.environ, values, clear=True), \
                 patch.object(publisher.shutil, 'which', return_value='/usr/bin/gh'), \
                 patch.object(publisher, 'view_release', return_value=owned), \
                 patch.object(publisher, 'remote_assets_match', return_value=False), \
                 patch.object(publisher, 'run_gh') as gh:
                with self.assertRaisesRegex(RuntimeError, 'different ZIP'):
                    publisher.main()
                gh.assert_not_called()

    def test_new_release_is_draft_verified_then_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            mkzip(Path(tmp)/'Portable.zip')
            values = dict(PUBLISH_FILES='Portable.zip', PUBLISH_TAG='portable-v1-r1',
                          PUBLISH_DRY_RUN='false', ACTION_EVENT='workflow_dispatch',
                          WORKSPACE_ROOT=tmp, TARGET_REPO='vhick/test-builder',
                          GH_TOKEN='pretend', SOURCE_SHA='abcd')
            with patch.dict(os.environ, values, clear=True), \
                 patch.object(publisher.shutil, 'which', return_value='/usr/bin/gh'), \
                 patch.object(publisher, 'view_release', return_value=None), \
                 patch.object(publisher, 'verify_new_draft') as verify, \
                 patch.object(publisher, 'run_gh') as gh:
                self.assertEqual(publisher.main(), 0)
                self.assertEqual(gh.call_count, 2)
                self.assertEqual(gh.call_args_list[0].args[:3], ('release','create','portable-v1-r1'))
                self.assertIn('--draft', gh.call_args_list[0].args)
                self.assertEqual(gh.call_args_list[1].args[:3], ('release','edit','portable-v1-r1'))
                verify.assert_called_once()

    def test_symlink_zip_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mkzip(base/'real.zip')
            (base/'linked.zip').symlink_to(base/'real.zip')
            with self.assertRaisesRegex(ValueError, 'No finished ZIP'):
                publisher.collect_zips('linked.zip', base)


if __name__ == '__main__':
    unittest.main()
