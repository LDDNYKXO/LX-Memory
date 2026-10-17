#!/usr/bin/env python
"""Pre-commit gate for LX Memory project handoffs.

Usage:
  python tools/validate_proposal.py --root <connector-root> --proposal <id>
  python tools/validate_proposal.py --root <connector-root> --file draft.json

Exit codes: 0 = ok/not_applicable, 1 = blocked, 2 = invocation/runtime error.
The tool is read-only: it calls the connected broker's `validate` operation,
never prepare/commit and never writes the cloud.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'runtime' / 'deps')]
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError, OSError):
    pass

from lingxi_connector.broker_client import call  # noqa: E402


def _load_proposal(connector_root, proposal_id):
    path = Path(connector_root) / 'runtime' / 'session' / 'writeback' / f'{proposal_id}.proposal.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    payload = value.get('payload') if isinstance(value, dict) else None
    if not isinstance(payload, dict):
        raise ValueError('proposal payload missing')
    target = payload.get('path') or ''
    parts = target.split('/')
    category = parts[1] if len(parts) > 1 else ''
    front = payload.get('front_matter') or {}
    return {
        'category': category,
        'title': front.get('title') or '',
        'summary': front.get('summary') or '',
        'content': payload.get('content') or '',
        'path': target,
    }


def _load_file(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('draft file must contain a JSON object')
    return {
        'category': value.get('category') or 'projects',
        'title': value.get('title') or '',
        'summary': value.get('summary') or '',
        'content': value.get('content') or '',
        'path': value.get('path'),
        'project': value.get('project'),
        'cwd': value.get('cwd') or '',
        'remote': value.get('remote') or '',
    }


def main():
    parser = argparse.ArgumentParser(description='Validate an LX Memory handoff before commit.')
    parser.add_argument('--root', default=os.environ.get('LX_MEMORY_ROOT', ''),
                        help='Connector root containing runtime/session (default: LX_MEMORY_ROOT)')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--proposal', help='prepared proposal id under runtime/session/writeback')
    source.add_argument('--file', help='JSON draft file (category/title/summary/content)')
    args = parser.parse_args()
    if not args.root:
        parser.error('--root or LX_MEMORY_ROOT is required')
    try:
        draft = _load_proposal(args.root, args.proposal) if args.proposal else _load_file(args.file)
        result = call(Path(args.root), 'validate', draft)
    except Exception as error:  # CLI boundary: return a machine-readable failure.
        print(json.dumps({'state': 'error', 'reason': type(error).__name__, 'detail': str(error)},
                         ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('state') in ('ok', 'not_applicable') else 1


if __name__ == '__main__':
    raise SystemExit(main())
