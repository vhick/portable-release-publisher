import importlib.util
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'publish.py'
spec = importlib.util.spec_from_file_location('publisher_under_test', SCRIPT)
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)


def zip_package(path, content='hello'):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('readme.txt', content)


def paf_stub(path, content=b'PortableApps NSIS test stub'):
    # Header-level synthetic PE fixture, NOT an executable test installer.
    header = bytearray(132)
    header[0:2] = b'MZ'
    struct.pack_into('<I', header, 0x3C, 128)
    header[128:132] = b'PE\x00\x00'
    path.write_bytes(header + content)


class AssetsTests(unittest.TestCase):
    def test_zip_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            zip_package(root / 'AppNative.zip')
            self.assertEqual([p.name for p in pub.collect_assets('*.zip', root)], ['AppNative.zip'])

    def test_paf_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paf_stub(root / 'AppPortable_1.2.paf.exe')
            self.assertEqual([p.name for p in pub.collect_assets('*.paf.exe', root)], ['AppPortable_1.2.paf.exe'])

    def test_mixed_assets_and_checksums(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'dist').mkdir()
            zip_package(root/'dist'/'App.zip')
            paf_stub(root/'dist'/'App.paf.exe')
            files = pub.collect_assets('dist/*.zip;dist/*.paf.exe', root)
            self.assertEqual([p.name for p in files], ['App.paf.exe', 'App.zip'])
            sums = pub.checksums(files)
            self.assertEqual(len(sums.splitlines()), 2)
            self.assertIn('App.paf.exe', sums)
            self.assertIn('App.zip', sums)

    def test_broad_glob_ignores_normal_exe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paf_stub(root/'App.paf.exe')
            (root/'regular-installer.exe').write_bytes(b'not PAF')
            (root/'notes.txt').write_text('notes')
            self.assertEqual([p.name for p in pub.collect_assets('*', root)], ['App.paf.exe'])

    def test_case_insensitive_extension(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            paf_stub(root/'APP.PAF.EXE')
            self.assertEqual([p.name for p in pub.collect_assets('*', root)], ['APP.PAF.EXE'])

    def test_cannot_publish_invalid_paf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'App.paf.exe').write_bytes(b'not an installer')
            with self.assertRaisesRegex(ValueError, r'Invalid \.paf\.exe'):
                pub.collect_assets('*.paf.exe', root)

    def test_mz_without_pe_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'App.paf.exe').write_bytes(b'MZ' + b'\x00' * 300)
            with self.assertRaisesRegex(ValueError, 'Invalid'):
                pub.collect_assets('*.paf.exe', root)

    def test_corrupt_zip_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'bad.zip').write_text('broken')
            with self.assertRaisesRegex(ValueError, 'Invalid ZIP'):
                pub.collect_assets('*.zip', root)

    def test_no_package_found(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'No .zip or .paf.exe'):
                pub.collect_assets('dist/*.zip;dist/*.paf.exe', Path(temp))

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for path in ('../*.zip', '..\\*.paf.exe', 'C:/dist/*.paf.exe', '/tmp/*.zip'):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Unsafe'):
                    pub.collect_assets(path, Path(temp))

    def test_duplicate_asset_names_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'one').mkdir(); (root/'two').mkdir()
            paf_stub(root/'one'/'Same.paf.exe')
            paf_stub(root/'two'/'Same.paf.exe')
            with self.assertRaisesRegex(ValueError, 'Duplicate asset'):
                pub.collect_assets('one/*;two/*', root)

    def test_symlink_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            paf_stub(root/'real.paf.exe')
            (root/'link.paf.exe').symlink_to(root/'real.paf.exe')
            with self.assertRaisesRegex(ValueError, 'No .zip'):
                pub.collect_assets('link.paf.exe', root)

    def test_collect_zips_backward_compatibility(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            zip_package(root/'old.zip')
            self.assertEqual(pub.collect_zips('*.zip', root), pub.collect_assets('*.zip', root))

    def test_one_unsupported_exe_not_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            paf_stub(root/'Setup.exe')
            with self.assertRaisesRegex(ValueError, 'No .zip'):
                pub.collect_assets('Setup.exe', root)


class ReleaseFlowTests(unittest.TestCase):
    def test_tag_validation(self):
        pub.validate_tag('portable-v1.2.3-r1','portable-')
        with self.assertRaisesRegex(ValueError, 'begin'):
            pub.validate_tag('v1.2.3', 'portable-')

    def test_dry_run_mixed_has_no_gh_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            zip_package(root/'App.zip')
            paf_stub(root/'App.paf.exe')
            env={'PUBLISH_FILES':'*.zip;*.paf.exe','PUBLISH_TAG':'portable-v1',
                 'PUBLISH_DRY_RUN':'true','WORKSPACE_ROOT':temp,
                 'GH_TOKEN':'','TARGET_REPO':''}
            with patch.dict(os.environ, env, clear=True), patch.object(pub, 'run_gh') as mocked:
                self.assertEqual(pub.main(), 0)
                mocked.assert_not_called()

    def test_existing_release_skips_matching_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            zip_package(root/'App.zip')
            paf_stub(root/'App.paf.exe')
            release={'isDraft':False,'body':pub.MARKER}
            env={'PUBLISH_FILES':'*.zip;*.paf.exe','PUBLISH_TAG':'portable-v1',
                 'PUBLISH_DRY_RUN':'false','WORKSPACE_ROOT':temp,
                 'GH_TOKEN':'dummy','SOURCE_SHA':'abc',
                 'ACTION_EVENT':'workflow_dispatch','TARGET_REPO':'vhick/Test'}
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(pub.shutil,'which',return_value='gh'), \
                 patch.object(pub,'view_release',return_value=release), \
                 patch.object(pub,'remote_assets_match',return_value=True), \
                 patch.object(pub,'run_gh') as mocked:
                self.assertEqual(pub.main(), 0)
                mocked.assert_not_called()

    def test_existing_release_conflict_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            paf_stub(root/'App.paf.exe')
            env={'PUBLISH_FILES':'*.paf.exe','PUBLISH_TAG':'portable-v1',
                 'PUBLISH_DRY_RUN':'false','WORKSPACE_ROOT':temp,
                 'GH_TOKEN':'dummy','SOURCE_SHA':'abc',
                 'ACTION_EVENT':'workflow_dispatch','TARGET_REPO':'vhick/Test'}
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(pub.shutil,'which',return_value='gh'), \
                 patch.object(pub,'view_release',return_value={'isDraft':False,'body':pub.MARKER}), \
                 patch.object(pub,'remote_assets_match',return_value=False):
                with self.assertRaisesRegex(RuntimeError,'different assets'):
                    pub.main()

    def test_new_draft_uploads_zip_and_paf_and_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            zip_package(root/'App.zip')
            paf_stub(root/'App.paf.exe')
            env={'PUBLISH_FILES':'*.zip;*.paf.exe','PUBLISH_TAG':'portable-v1',
                 'PUBLISH_DRY_RUN':'false','WORKSPACE_ROOT':temp,
                 'GH_TOKEN':'dummy','SOURCE_SHA':'abc',
                 'ACTION_EVENT':'workflow_dispatch','TARGET_REPO':'vhick/Test'}
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(pub.shutil,'which',return_value='gh'), \
                 patch.object(pub,'view_release',return_value=None), \
                 patch.object(pub,'verify_new_draft') as verify, \
                 patch.object(pub,'run_gh') as calls:
                self.assertEqual(pub.main(),0)
                args=calls.call_args_list[0].args
                self.assertEqual(args[:2],('release','create'))
                self.assertIn('--draft',args)
                self.assertTrue(any(str(a).endswith('.paf.exe') for a in args))
                self.assertTrue(any(str(a).endswith('.zip') for a in args))
                self.assertTrue(any(str(a).endswith('SHA256SUMS.txt') for a in args))
                verify.assert_called_once()
                self.assertEqual(calls.call_args_list[1].args[:2],('release','edit'))

    def test_existing_draft_not_modified(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            paf_stub(root/'App.paf.exe')
            env={'PUBLISH_FILES':'*.paf.exe','PUBLISH_TAG':'portable-v1',
                 'PUBLISH_DRY_RUN':'false','WORKSPACE_ROOT':temp,
                 'GH_TOKEN':'dummy','SOURCE_SHA':'abc',
                 'ACTION_EVENT':'workflow_dispatch','TARGET_REPO':'vhick/Test'}
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(pub.shutil,'which',return_value='gh'), \
                 patch.object(pub,'view_release',return_value={'isDraft':True,'body':pub.MARKER}):
                with self.assertRaisesRegex(RuntimeError,'Existing draft'):
                    pub.main()

    def test_prune_excludes_manual_and_immutable(self):
        entries=[{'tagName':f'portable-v{v}', 'publishedAt': f'2026-01-{v:02d}',
                  'isImmutable': v==1, 'isDraft':False} for v in range(1,5)]
        entries.append({'tagName':'manual-v1','publishedAt':'2026-01-06','isImmutable':False,'isDraft':False})
        self.assertEqual(pub.choose_old_releases(entries,'portable-','portable-v4',2,lambda _: True), ['portable-v2'])

    def test_pull_request_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            paf_stub(root/'App.paf.exe')
            env={'PUBLISH_FILES':'*.paf.exe','PUBLISH_TAG':'portable-v1',
                 'PUBLISH_DRY_RUN':'false','WORKSPACE_ROOT':temp,
                 'ACTION_EVENT':'pull_request'}
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError,'Unsafe publishing event'):
                    pub.main()


if __name__ == '__main__':
    unittest.main()
