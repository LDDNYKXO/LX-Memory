"""Read-only inspection plus explicit, reversible configuration transactions."""
import tomllib
import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

import yaml

from .setup import InstallerError, load_json

PRODUCT = 'lingxi-memory'
DSH_ENTRY_ID = 'mcp-' + PRODUCT
SCHEMA = 'lingxi-memory/capability-matrix/v1'
MECHANISMS = ('mcp_tool', 'rules_injection', 'lifecycle')
CAPABILITY_FILE = Path(__file__).resolve().parents[2] / 'integrations' / 'capability-matrix.json'

# Mechanism and auto_bootstrap are design declarations, never native acceptance evidence.
DECLARED = {
    'Codex': {'mechanism': 'rules_injection', 'instruction_path': '~/.codex/AGENTS.md',
              'auto_bootstrap': True, 'config_path': '~/.codex/config.toml'},
    'DSH': {'mechanism': 'lifecycle', 'instruction_path': '~/.dsh/AGENTS.md',
            'auto_bootstrap': True, 'config_path': '~/.dsh/profiles/desktop/cordis.patch.yml'},
    'ZCode': {'mechanism': 'mcp_tool', 'instruction_path': '~/.zcode/AGENTS.md',
              'auto_bootstrap': True, 'config_path': '~/.zcode/cli/config.json'},
    'WorkBuddy': {'mechanism': 'mcp_tool',
                  'instruction_path': '~/.workbuddy/skills/lingxi-cloud-memory/SKILL.md',
                  'auto_bootstrap': False, 'config_path': '~/.workbuddy/mcp.json'},
}


def declared_matrix():
    """The committed declaration: mechanism and auto-trigger only, never absolute user paths."""
    return {'schema': SCHEMA, 'mechanisms': list(MECHANISMS),
            'clients': {name: dict(row) for name, row in DECLARED.items()}}


def expand(home, template):
    return Path(home) / template.replace('~/', '').replace('~\\', '')


def _read(path):
    try:
        return Path(path).read_text(encoding='utf-8-sig')
    except OSError:
        return None


def _inside(path, roots):
    """True only when `path` really lives under a root: component-wise, never a string prefix.

    `…\\product-backup\\run.py` must stay foreign to `…\\product`; a prefix test would adopt it.
    """
    try:
        candidate = Path(str(path))
    except (TypeError, ValueError):
        return False
    for root in roots:
        root = Path(str(root))
        if candidate == root or root in candidate.parents:
            return True
    return False


def _mcp_configured(client, config, root):
    """Our MCP entry exists in this host config *and* points at this very root."""
    config = Path(config)
    text = _read(config)
    if text is None:
        return False
    try:
        entry, _ = _entries(client, config, text)
    except InstallerError:
        return False
    return any(_inside(arg, [root]) for arg in _connector_args(entry))


def _instruction_configured(instruction):
    text = _read(instruction)
    return text is not None and PRODUCT in text


def capability_matrix(home, root):
    """Runtime view: the declared mechanism plus what is actually present on this machine.

    `verified_native` stays False by construction: a present configuration is not a native
    acceptance result, and this module has no way to run one.
    """
    home, root = Path(home), Path(root)
    clients = {}
    for name, row in DECLARED.items():
        config = expand(home, row['config_path'])
        instruction = expand(home, row['instruction_path'])
        mcp_configured = _mcp_configured(name, config, root)
        instruction_configured = _instruction_configured(instruction)
        lifecycle_configured = False
        if name == 'DSH':
            try:
                parsed = _yaml_load(_read(config) or '')
                def entries(value):
                    if isinstance(value, list):
                        for child in value:
                            yield from entries(child)
                    elif isinstance(value, dict):
                        yield value
                        for key in ('insert', 'children', 'config'):
                            yield from entries(value.get(key))
                expected = (root / 'integrations/dsh/index.mjs').resolve().as_uri()
                lifecycle_configured = any(item.get('id') == 'lingxi-memory-pre-step' and
                    item.get('name') == expected and
                    (item.get('config') or {}).get('entry') == str(root / 'run.py')
                    for item in entries(parsed))
            except (ValueError, TypeError, AttributeError, yaml.YAMLError):
                lifecycle_configured = False
        elif name == 'WorkBuddy':
            text = _read(home / '.workbuddy/settings.json') or ''
            lifecycle_configured = str(root).replace('\\', '/').casefold() in text.replace('\\', '/').casefold() and 'UserPromptSubmit' in text
        clients[name] = {'client': name, 'mechanism': row['mechanism'],
                         'auto_bootstrap': row['auto_bootstrap'],
                         'config_path': str(config), 'instruction_path': str(instruction),
                         'configured': mcp_configured and {'mcp_tool': True,
                             'rules_injection': instruction_configured,
                             'lifecycle': lifecycle_configured}[row['mechanism']],
                         'lifecycle_configured': lifecycle_configured,
                         'automatic_recall_verified': False,
                         'mcp_configured': mcp_configured,
                         'instruction_configured': instruction_configured,
                         'instruction_present': instruction.is_file(),
                         'verified_native': False}
    return {'schema': SCHEMA, 'mechanisms': list(MECHANISMS), 'root': str(root), 'clients': clients}


def _yaml_load(text):
    class TaggedScalar(str):
        """Opaque passthrough for DSH extension tags such as `!!js dshHomePath(...)`."""

    class StrictLoader(yaml.SafeLoader):
        pass

    StrictLoader.add_constructor('tag:yaml.org,2002:js',
                                 lambda loader, node: TaggedScalar(loader.construct_scalar(node)))
    return yaml.load(text, Loader=StrictLoader) if text.strip() else []


def _format_error(client):
    return InstallerError('%s 配置格式不明，未修改' % client)


def _entries(client, path, text):
    """Return `(product_entry_or_None, unrelated_entry_count)`; refuse anything unexpected."""
    suffix = path.suffix.casefold()
    if suffix == '.toml':
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise _format_error(client) from error
        servers = data.get('mcp_servers') if isinstance(data, dict) else None
        if servers is None:
            servers = {}
        if not isinstance(servers, dict):
            raise _format_error(client)
        return servers.get(PRODUCT), len(servers) - (1 if PRODUCT in servers else 0)
    if suffix == '.json':
        try:
            data = load_json(text.encode('utf-8'))
        except (ValueError, TypeError) as error:
            raise _format_error(client) from error
        if not isinstance(data, dict):
            raise _format_error(client)
        keys = ('mcpServers',) if client == 'WorkBuddy' else ('mcp', 'servers')
        cursor = data
        for key in keys:
            cursor = cursor.get(key) if isinstance(cursor, dict) else None
            if cursor is None:
                return None, 0
        if not isinstance(cursor, dict):
            raise _format_error(client)
        return cursor.get(PRODUCT), len(cursor) - (1 if PRODUCT in cursor else 0)
    if suffix in ('.yml', '.yaml'):
        try:
            data = _yaml_load(text)
        except yaml.YAMLError as error:
            raise _format_error(client) from error
        if not isinstance(data, list):
            raise _format_error(client)
        items = []
        def visit(value):
            if isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, dict):
                if 'name' in value:
                    items.append(value)
                for key in ('insert', 'children', 'config'):
                    child = value.get(key)
                    if isinstance(child, (list, dict)):
                        visit(child)
        visit(data)
        matches = [item for item in items if item.get('id') == DSH_ENTRY_ID or
                   (isinstance(item.get('config'), dict) and item['config'].get('serverName') == PRODUCT)]
        if len(matches) > 1:
            raise InstallerError('DSH_DUPLICATE_PRODUCT')
        product = matches[0] if matches else None
        return product, len(items) - (1 if product is not None else 0)
    raise _format_error(client)


def _connector_args(entry):
    if not isinstance(entry, dict):
        return []
    config = entry.get('config')
    candidate = config.get('args') if isinstance(config, dict) else entry.get('args')
    return [str(item) for item in candidate] if isinstance(candidate, list) else []


def verify_ownership(paths, owned_roots):
    """Report what each host config holds: absent, ours (owned), foreign, or unrelated-only."""
    roots = [str(Path(root)) for root in owned_roots]
    report = {'schema': 'lingxi-memory/ownership-check/v1', 'written': False, 'clients': {}}
    for client, path in paths.items():
        path = Path(path)
        text = _read(path)
        if text is None:
            report['clients'][client] = {'path': str(path), 'state': 'absent',
                                         'already_configured': False, 'preserved': 0, 'owned': False}
            continue
        entry, preserved = _entries(client, path, text)
        args = _connector_args(entry)
        owned = any(_inside(arg, roots) for arg in args)
        state = 'unrelated-only' if entry is None else ('owned' if owned else 'foreign')
        report['clients'][client] = {'path': str(path), 'state': state,
                                     'already_configured': state == 'owned',
                                     'preserved': preserved, 'owned': owned, 'owned_roots': roots}
    return report


RULE_START = '<!-- lingxi-memory shared-resume: v1 -->'
RULE_END = '<!-- end lingxi-memory shared-resume -->'
RULE_PATHS = {'Codex': '.codex/AGENTS.md', 'DSH': '.dsh/AGENTS.md', 'ZCode': '.zcode/AGENTS.md'}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _atomic_bytes(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.lingxi-' + secrets.token_hex(8) + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def configure_rules(root, home, clients, apply=False):
    """Install one shared rule artifact as an owned block, with genuine preimages.

    Existing legacy pointers are retained; a block changed by a user is never silently adopted.
    This configures instructions, not native acceptance or an automatic-trigger guarantee.
    """
    from .setup import safe_target
    from .broker import private_directory
    root, home = Path(root).resolve(), Path(home).resolve()
    body = (root / 'integrations/resume-rules.md').read_text(encoding='utf-8').strip()
    block = (RULE_START + '\n' + body + '\n' + RULE_END + '\n').encode('utf-8')
    try:
        history = json.loads((root / 'integrations/rules-history.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        history = {}
    known = history.get('block_sha256', []) if isinstance(history, dict) and history.get('schema') == 1 else []
    if not isinstance(known, list):
        known = []
    mutations = []
    for client in dict.fromkeys(clients):
        if client not in DECLARED:
            raise InstallerError('UNKNOWN_CLIENT')
        if client not in RULE_PATHS:
            continue  # WorkBuddy uses a lifecycle plugin, never a made-up global rule path.
        target = home / RULE_PATHS[client]
        safe_target(target, home)
        before = target.read_bytes() if target.exists() else None
        text = (before or b'').decode('utf-8-sig')
        if RULE_START in text or RULE_END in text:
            if text.count(RULE_START) != 1 or text.count(RULE_END) != 1:
                raise InstallerError('RULE_UNKNOWN_MODIFICATION')
            start = before.index(RULE_START.encode())
            end = before.index(RULE_END.encode()) + len(RULE_END.encode())
            if end < start or before[end:end+1] != b'\n':
                raise InstallerError('RULE_UNKNOWN_MODIFICATION')
            end += 1
            previous = before[start:end]
            if previous == block:
                continue
            if _digest(previous) not in known:
                raise InstallerError('RULE_UNKNOWN_MODIFICATION')
            mutations.append((target, before, before[:start] + block + before[end:]))
            continue
        after = (before or b'') + (b'\n' if before and not before.endswith(b'\n') else b'') + block
        mutations.append((target, before, after))
    return _commit_mutations(root, home, mutations, apply)


def _commit_mutations(root, home, mutations, apply):
    from .setup import safe_target
    from .broker import private_directory
    report = {'changed': len(mutations), 'applied': False, 'journal': None,
              'targets': [{'path': str(p), 'before_sha256': _digest(a), 'after_sha256': _digest(b)}
                          for p, a, b in mutations]}
    if not apply or not mutations:
        return report
    directory = root / 'runtime/session/rule-transactions'
    private_directory(directory)
    journal = directory / (secrets.token_hex(12) + '.json')
    entries = [{**entry, 'before_base64': base64.b64encode(a).decode('ascii') if a is not None else None}
               for entry, (_, a, _) in zip(report['targets'], mutations)]
    _atomic_bytes(journal, json.dumps({'schema': 'lingxi/rules-transaction/v1', 'entries': entries},
                                     ensure_ascii=False).encode('utf-8'))
    # Recheck the complete write set before the first mutation.
    for target, before, _ in mutations:
        safe_target(target, home)
        if (target.read_bytes() if target.exists() else None) != before:
            raise InstallerError('RULE_CONCURRENT_CHANGE')
    for target, before, after in mutations:
        if (target.read_bytes() if target.exists() else None) != before:
            raise InstallerError('RULE_CONCURRENT_CHANGE: journal=' + str(journal))
        _atomic_bytes(target, after)
        if target.read_bytes() != after:
            raise InstallerError('RULE_READBACK_FAILED')
    report.update(applied=True, journal=str(journal))
    return report


def configure_lifecycle(root, home, python, clients, apply=False):
    """Reversible thin adapters; never replace unrelated hooks or change model settings."""
    from .setup import safe_target
    import shlex
    root, home, python = Path(root).resolve(), Path(home).resolve(), Path(python).resolve()
    mutations = []
    for client in dict.fromkeys(clients):
        if client not in ('DSH', 'WorkBuddy'):
            raise InstallerError('LIFECYCLE_HOST_UNSUPPORTED')
        relative = '.workbuddy/settings.json' if client == 'WorkBuddy' else '.dsh/profiles/desktop/cordis.patch.yml'
        target = home / relative; safe_target(target, home)
        before = target.read_bytes() if target.exists() else None
        if client == 'WorkBuddy':
            data = load_json(before) if before is not None else {}
            if not isinstance(data, dict) or data.get('disableAllHooks') is True or data.get('allowManagedHooksOnly') is True:
                raise InstallerError('LIFECYCLE_DISABLED_OR_UNSUPPORTED')
            hooks = data.setdefault('hooks', {})
            if not isinstance(hooks, dict):
                raise InstallerError('LIFECYCLE_UNKNOWN_FORMAT')
            groups = hooks.setdefault('UserPromptSubmit', [])
            if not isinstance(groups, list):
                raise InstallerError('LIFECYCLE_UNKNOWN_FORMAT')
            command = ' '.join(shlex.quote(path.as_posix()) for path in (python, root / 'run.py')) + ' hook'
            group = {'matcher': '', 'hooks': [{'type': 'command', 'command': command, 'timeout': 50}]}
            if group in groups:
                continue
            if any('lingxi' in json.dumps(item).casefold() and ' hook' in json.dumps(item) for item in groups):
                raise InstallerError('LIFECYCLE_FOREIGN_ENTRY')
            groups.append(group)
            after = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        else:
            if not (root / 'integrations/dsh/index.mjs').is_file():
                raise InstallerError('LIFECYCLE_ADAPTER_MISSING')
            text = (before or b'').decode('utf-8-sig')
            parsed = _yaml_load(text)
            if not isinstance(parsed, list):
                raise InstallerError('LIFECYCLE_UNKNOWN_FORMAT')
            entry = [{'insert': [{'id': 'lingxi-memory-pre-step',
                      'name': (root / 'integrations/dsh/index.mjs').as_uri(),
                      'config': {'python': str(python), 'entry': str(root / 'run.py')}}]}]
            block = ('\n# begin lingxi-memory lifecycle v1\n' + yaml.safe_dump(entry, allow_unicode=True, sort_keys=False)
                     + '# end lingxi-memory lifecycle v1\n')
            if block.strip() in text:
                continue
            if 'lingxi-memory-pre-step' in text:
                raise InstallerError('LIFECYCLE_FOREIGN_ENTRY')
            after = (text + block).encode('utf-8')
            if _yaml_load(after.decode()) != parsed + entry:
                raise InstallerError('LIFECYCLE_PARSE_CHANGED')
        mutations.append((target, before, after))
    return _commit_mutations(root, home, mutations, apply)


def rollback_rules(root, home, journal):
    """Restore after-state preimages, refusing drift or arbitrary journal paths."""
    from .setup import safe_target
    root, home, journal = Path(root).resolve(), Path(home).resolve(), Path(journal)
    directory = root / 'runtime/session/rule-transactions'
    if journal.is_symlink() or journal.resolve().parent != directory.resolve():
        raise InstallerError('RULE_JOURNAL_OUTSIDE_PRODUCT')
    data = load_json(journal.read_bytes())
    if data.get('schema') != 'lingxi/rules-transaction/v1':
        raise InstallerError('RULE_JOURNAL_INVALID')
    restored = []
    allowed = {str(home / path) for path in (*RULE_PATHS.values(), '.workbuddy/settings.json', '.dsh/profiles/desktop/cordis.patch.yml')}
    for row in data['entries']:
        target = Path(row['path']); safe_target(target, home)
        if str(target) not in allowed:
            raise InstallerError('RULE_JOURNAL_INVALID_TARGET')
        before = base64.b64decode(row['before_base64'], validate=True) if row['before_base64'] is not None else None
        if _digest(before) != row['before_sha256']:
            raise InstallerError('RULE_BACKUP_HASH_MISMATCH')
        current = target.read_bytes() if target.exists() else None
        if _digest(current) == row['before_sha256']:
            continue  # safely resume a partially completed rollback
        if _digest(current) != row['after_sha256']:
            raise InstallerError('RULE_ROLLBACK_DRIFT')
        restored.append((target, current, before))
    for target, expected, before in restored:
        if target.read_bytes() != expected:
            raise InstallerError('RULE_ROLLBACK_DRIFT')
        if before is None:
            target.unlink()
        else:
            _atomic_bytes(target, before)
        if (target.read_bytes() if target.exists() else None) != before:
            raise InstallerError('RULE_ROLLBACK_READBACK_FAILED')
    return {'restored': len(restored), 'verified': True}
