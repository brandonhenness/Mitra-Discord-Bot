import hashlib
import io
import json
import shutil
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from mitra_bot import release_tools
from mitra_bot.services.ups.ups_database import UPSLogStore
from mitra_bot.services import update_service
from mitra_bot.setup_wizard import DiscordSetup, install_url, save_token


def sample(minutes=1, **extra):
    return dict(ts=(datetime.now(timezone.utc)-timedelta(minutes=minutes)).isoformat(), battery_percent=80, **extra)


def test_legacy_migration_retry_and_growing_file(tmp_path):
    legacy = tmp_path/'old.jsonl'
    contents = json.dumps(sample())+'\ninvalid\n'+json.dumps(sample(2))+'\n'
    legacy.write_text(contents, encoding='utf-8')
    store = UPSLogStore(log_file=str(legacy))
    assert store.log_path.suffix == '.db'
    assert len(store.get_recent(hours=24)) == 2
    assert legacy.read_text(encoding='utf-8') == contents
    with sqlite3.connect(store.log_path) as db:
        assert db.execute('select skipped from ups_imports').fetchone()[0] == 1
        db.execute('delete from ups_imports')  # Simulate interrupted import before journal completion.
    store = UPSLogStore(log_file=str(legacy))
    assert len(store.get_recent(hours=24)) == 2
    legacy.write_text(contents+json.dumps(sample(3))+'\n', encoding='utf-8')
    store = UPSLogStore(log_file=str(legacy))
    assert len(store.get_recent(hours=24)) == 3


def test_database_persistence_sampling_and_concurrent_writes(tmp_path):
    path = str(tmp_path/'ups.db')
    store = UPSLogStore(database_file=path, history_limit=10)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: store.append(sample(n+1, sequence=n)), range(80)))
    store = UPSLogStore(database_file=path, history_limit=10)
    rows = store.get_recent(hours=24)
    assert 2 <= len(rows) <= 10
    assert rows[0]['sequence'] == 79
    assert rows[-1]['sequence'] == 0
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from ups_samples').fetchone()[0] == 80
    assert len(store.get_recent(hours=0.5, limit=100)) < 30


def test_token_storage_preserves_other_settings_and_rejects_injection(tmp_path):
    path = tmp_path/'.env'
    path.write_text('# comment\nOTHER=value\nexport DISCORD_APPLICATION_TOKEN=old\n', encoding='utf-8')
    save_token(path, 'example.token_'+'x'*30)
    saved = path.read_text(encoding='utf-8')
    assert 'OTHER=value' in saved and '# comment' in saved
    assert saved.count('DISCORD_APPLICATION_TOKEN=') == 1
    with pytest.raises(ValueError):
        save_token(path, 'x'*30+'\nOTHER=changed')
    assert path.read_text(encoding='utf-8') == saved


def test_discord_url_permissions_and_api_error_hides_token(monkeypatch):
    query = parse_qs(urlparse(install_url('123456789')).query)
    assert query['scope'] == ['bot applications.commands']
    assert int(query['permissions'][0]) & 8 == 0
    assert 'token' not in query
    monkeypatch.setattr('mitra_bot.setup_wizard.requests.request', Mock(return_value=Mock(status_code=401)))
    with pytest.raises(RuntimeError, match='HTTP 401') as error:
        DiscordSetup('private-token').application()
    assert 'private-token' not in str(error.value)


def version_tree(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name in release_tools.VERSION_FILES:
        target = tmp_path/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root/name, target)
    return tmp_path


def test_release_stamp_bundle_and_checksum_exclude_runtime(tmp_path):
    root = version_tree(tmp_path/'source')
    release_tools.stamp(root, '9.1.0rc1')
    assert release_tools.check(root, 'v9.1.0rc1') == '9.1.0rc1'
    for name in ('.env','ups_stats.db','mitra_bot/secret.key','peer-network.toml'):
        (root/name).write_text('PRIVATE', encoding='utf-8')
    files = [p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()]
    output = tmp_path/'out'
    archive = release_tools.bundle(root, output, files=files, repository='owner/repo')
    with zipfile.ZipFile(archive) as zf:
        assert not any(b'PRIVATE' in zf.read(name) for name in zf.namelist())
        metadata = json.loads(zf.read('mitra-discord-bot-9.1.0rc1/release.json'))
        assert metadata['repository'] == 'owner/repo'
    release_tools.checksums(output)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in (output/'SHA256SUMS').read_text()
    with pytest.raises(ValueError):
        release_tools.stamp(root, '9.0.0')


@pytest.mark.parametrize('name', ['../.env','C:\\private.key','mitra_bot/../../.env','scripts/local.db-wal','docs/.env.secret','peer-network.toml'])
def test_release_allowlist_rejects_private_or_unsafe_paths(name):
    assert not release_tools.allowed_file(name)


def test_updater_prefers_verified_asset():
    payload = dict(tag_name='v1.0.0', zipball_url='legacy', assets=[
        dict(name='mitra-discord-bot-1.0.0.zip', browser_download_url='asset', digest='sha256:'+'a'*64)])
    release = update_service._release_info_from_payload(payload, 'owner/repo')
    assert release.zipball_url == 'asset'
    assert release.sha256 == 'a'*64


def test_updater_checksum_failure_never_installs(monkeypatch, tmp_path):
    monkeypatch.setattr(update_service.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(update_service.requests, 'get', Mock(return_value=Mock(content=b'bad')))
    copy = Mock()
    monkeypatch.setattr(update_service, '_copy_release_tree', copy)
    result = update_service.install_release(update_service.ReleaseInfo('1.0.0','url','url','',sha256='0'*64))
    assert not result.ok and 'checksum' in result.error
    copy.assert_not_called()


def test_update_copy_preserves_private_files(tmp_path):
    source, target = tmp_path/'source', tmp_path/'target'
    source.mkdir(); target.mkdir()
    for name in ('.env','ups_stats.db','peer-network.toml','node.key','config.toml','README.md'):
        (source/name).write_text('new', encoding='utf-8')
        (target/name).write_text('old', encoding='utf-8')
    update_service._copy_release_tree(source, target)
    assert (target/'README.md').read_text() == 'new'
    for name in ('.env','ups_stats.db','peer-network.toml','node.key','config.toml'):
        assert (target/name).read_text() == 'old'


def test_archive_traversal_rejected_before_copy(monkeypatch, tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('../outside.txt', 'bad')
    monkeypatch.setattr(update_service.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(update_service.requests, 'get', Mock(return_value=Mock(content=buffer.getvalue())))
    copy = Mock()
    monkeypatch.setattr(update_service, '_copy_release_tree', copy)
    result = update_service.install_release(update_service.ReleaseInfo('1.0.0','url','url',''))
    assert not result.ok and 'Unsafe path' in result.error
    copy.assert_not_called()


def test_guided_setup_configures_guild_without_opening_browser(monkeypatch, tmp_path):
    from mitra_bot import setup_wizard
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('DISCORD_APPLICATION_TOKEN', raising=False)
    monkeypatch.setenv('MITRA_CONFIG_PATH', str(tmp_path/'config.toml'))
    token = 'sample.'+'x'*35
    monkeypatch.setattr(setup_wizard.getpass, 'getpass', lambda _: token)
    browser = Mock()
    monkeypatch.setattr(setup_wizard.webbrowser, 'open', browser)
    replies = iter(['', '', '1', '1', 'y', 'n', '1', 'n'])
    monkeypatch.setattr('builtins.input', lambda _: next(replies))
    calls = []
    def api(self, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {
            ('GET','/oauth2/applications/@me'): {'id':'123','name':'Test'},
            ('GET','/users/@me/guilds'): [{'id':'456','name':'Guild'}],
            ('GET','/guilds/456'): {'owner_id':'789'},
            ('GET','/guilds/456/channels'): [{'id':'999','name':'alerts','type':0}],
            ('GET','/guilds/456/roles'): [],
            ('POST','/guilds/456/roles'): {'id':'888'},
            ('PUT','/guilds/456/members/789/roles/888'): None,
        }[method,path]
    monkeypatch.setattr(setup_wizard.DiscordSetup, 'request', api)
    channel_save = Mock()
    monkeypatch.setattr('mitra_bot.storage.storage_store.set_notification_channel_id_for_guild', channel_save)
    setup_wizard.guided_setup(open_browser=False)
    browser.assert_not_called()
    channel_save.assert_called_once_with(456,999)
    assert token in (tmp_path/'.env').read_text()
    config = setup_wizard.read_config_dict()
    assert config['bot']['channel_id'] == 999
    assert not config['ups']['enabled']
    assert any(call[:2] == ('PUT','/guilds/456/members/789/roles/888') for call in calls)


def test_invalid_token_does_not_replace_existing_secret(monkeypatch, tmp_path):
    from mitra_bot import setup_wizard
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('DISCORD_APPLICATION_TOKEN', raising=False)
    original = 'DISCORD_APPLICATION_TOKEN='+'x'*40+'\n'
    (tmp_path/'.env').write_text(original)
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    monkeypatch.setattr(setup_wizard.getpass, 'getpass', lambda _: 'invalid')
    monkeypatch.setattr(setup_wizard.DiscordSetup, 'application', Mock(side_effect=RuntimeError('HTTP 401')))
    with pytest.raises(RuntimeError):
        setup_wizard.guided_setup(open_browser=False)
    assert (tmp_path/'.env').read_text() == original


def test_peer_bundle_install_is_repeatable_and_refuses_overwrite(tmp_path):
    from mitra_bot.setup_wizard import install_bundle
    root = Path(__file__).resolve().parents[1]
    source, target = tmp_path/'bundle', tmp_path/'installed'
    source.mkdir()
    config = (root/'peer-network.example.toml').read_text().replace('enabled = false','enabled = true',1)
    (source/'peer-network.toml').write_text(config)
    for name in ('ca.crt','node.crt','node.key'):
        (source/name).write_text(name)
    install_bundle(source, target)
    install_bundle(source, target)
    (source/'node.key').write_text('different')
    with pytest.raises(ValueError, match='differs'):
        install_bundle(source, target)
    assert (target/'node.key').read_text() == 'node.key'


def test_published_release_is_never_mutated(monkeypatch, tmp_path):
    root = version_tree(tmp_path/'source')
    monkeypatch.delenv('GITHUB_EVENT_NAME', raising=False)
    command = Mock(return_value=json.dumps([{'tagName':'v99.0.0','isDraft':False}]))
    monkeypatch.setattr(release_tools.subprocess, 'check_output', command)
    with pytest.raises(ValueError, match='already published'):
        release_tools.publish(root,tmp_path/'dist','source')
    assert command.call_count == 1  # Only the read-only release listing happened.


def test_zip_install_remembers_repository(monkeypatch, tmp_path):
    monkeypatch.setattr(update_service,'PROJECT_ROOT',tmp_path)
    monkeypatch.setattr(update_service,'get_updater_config',lambda: {})
    monkeypatch.setattr(update_service,'_resolve_repo_from_git',lambda: None)
    (tmp_path/'release.json').write_text(json.dumps({'repository':'owner/repo'}))
    assert update_service.resolve_github_repo() == 'owner/repo'
