"""Short-lived previews and at-most-once submission; never infer user consent."""
import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from .cloud import ConnectorError, read_url
from . import page_meta
from .prepare_assessment import assess
from .project_identity import normalize_name
from .project_record import parse_record, record_policy
from .proposal_validation import validate_proposal

ASSESSMENT_TIMEOUT = 30
ASSESSMENT_READ_CHARS = 12000

# Scopes that must never be deleted or replaced by this product, whatever the caller asks.
PROTECTED_PREFIXES = ('core/', 'wiki/profile/')
PROJECT_PATH_PREFIX = 'wiki/projects/'
RECORD_KIND_TEST = 'test'
RECORD_KIND_HISTORY = 'history'
RECORD_KIND_HANDOFF = 'handoff'


def _is_protected(path):
    return not isinstance(path, str) or any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def _marker_line(content):
    """Return the raw marker dict of the first line, or None. Structure is not validated here."""
    if not isinstance(content, str):
        return None
    stripped = content.lstrip()
    first = stripped.splitlines()[0] if stripped.splitlines() else ''
    prefix = '<!-- memory-project-record '
    if len(first) > 2048 or not first.startswith(prefix) or not first.endswith(' -->'):
        return None
    try:
        value = json.loads(first[len(prefix):-4])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _strict_record(content):
    """Return (marker, project_key) only for a structurally valid project record.

    Reuses the project-record parser so archive evidence follows the same schema as
    context/project resolution: schema=1, project name, a unique list of valid full
    replacement paths (max 4), timezone-aware as_of and a supported record kind.
    """
    raw = _marker_line(content)
    if raw is None or type(raw.get('schema')) is not int or raw['schema'] != 1:
        return None
    project = raw.get('project')
    if not isinstance(project, str) or not project.strip():
        return None
    key = normalize_name(project)
    if not key:
        return None
    parsed = parse_record(content, {key})
    if parsed is None:
        return None
    policy = record_policy(content, {key}, datetime.now().astimezone())
    if policy.get('time_state') == 'invalid':
        return None
    parsed = dict(parsed)
    parsed['record_kind'] = policy.get('record_kind')
    return parsed, key


def _strict_evidence(content, target_path, project_key):
    """Return the strict handoff record when it is project evidence for `target_path`."""
    strict = _strict_record(content)
    if strict is None:
        return None
    marker, key = strict
    if project_key is not None and key != project_key:
        return None
    if marker.get('record_kind') != RECORD_KIND_HANDOFF:
        return None
    if target_path not in marker['supersedes']:
        return None
    return marker


def _record_as_of(content):
    strict = _strict_record(content)
    return strict[0]['as_of'] if strict else None


def _front_matter_view(front_matter):
    """Canonical view of the four fields this product manages on a page."""
    fm = front_matter if isinstance(front_matter, dict) else {}

    def strings(key):
        values = fm.get(key)
        return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []

    return {'title': fm.get('title') if isinstance(fm.get('title'), str) else '',
            'summary': fm.get('summary') if isinstance(fm.get('summary'), str) else '',
            'sources': strings('sources'),
            'related': strings('related')}

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

class Writeback:
    def __init__(self, session, directory, clock=time.monotonic):
        self.session = session
        self.directory = Path(directory) / 'writeback'
        self.clock = clock
        self.proposals = {}
        self.lock = asyncio.Lock()

    def _file(self, identifier, kind):
        if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
            raise ConnectorError('INVALID_INPUT')
        self.directory.mkdir(exist_ok=True)
        if self.directory.is_symlink():
            raise ConnectorError('INVALID_INPUT')
        path = self.directory / (identifier + '.' + kind + '.json')
        if path.is_symlink():
            raise ConnectorError('INVALID_INPUT')
        return path

    def _save(self, identifier, kind, value):
        path = self._file(identifier, kind)
        temporary = path.with_suffix('.tmp')
        if temporary.is_symlink():
            raise ConnectorError('INVALID_INPUT')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    def _load(self, identifier, kind):
        path = self._file(identifier, kind)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, OSError):
            raise ConnectorError('INVALID_RECEIPT') from None

    async def _assessment(self, category, title, summary, content):
        pages, unknowns, related = [], [], []
        async def collect():
            nonlocal related
            matches = await self.session.search(title, 3)
            if not isinstance(matches, dict) or not isinstance(matches.get('results'), list):
                unknowns.append('search_response_invalid')
                return
            related = matches['results'][:3]
            if category != 'projects':
                return
            if matches.get('source') != 'lingxi_cloud':
                unknowns.append('search_source_unverified')
            total = matches.get('total')
            if type(total) is not int or total < len(matches['results']):
                unknowns.append('search_total_invalid')
            elif total > len(matches['results']):
                unknowns.append('search_candidates_not_fully_returned')
            if len(matches['results']) > 3:
                unknowns.append('candidate_limit_exceeded')
            remaining, seen = ASSESSMENT_READ_CHARS, set()
            for hit in related:
                path = hit.get('path') if isinstance(hit, dict) else None
                try:
                    read_url(path)
                    if not path.startswith('wiki/projects/'):
                        raise ConnectorError('INVALID_INPUT')
                except ConnectorError:
                    unknowns.append('candidate_path_invalid')
                    continue
                if path in seen:
                    continue
                seen.add(path)
                if remaining <= 0:
                    unknowns.append('read_budget_exhausted')
                    break
                allowance = min(4000, remaining)
                remaining -= allowance
                try:
                    page = await self.session.read(path, 0, allowance)
                    if not isinstance(page, dict) or page.get('path') != path:
                        raise ConnectorError('UPSTREAM_INCOMPATIBLE')
                    pages.append(page)
                except ConnectorError:
                    pages.append({'path': path, 'assessment_error': 'read_failed'})
        try:
            await asyncio.wait_for(collect(), timeout=ASSESSMENT_TIMEOUT)
        except (ConnectorError, asyncio.TimeoutError):
            unknowns.append('search_or_read_unavailable_or_timeout')
        return assess(category, title, summary, content, pages, unknowns=unknowns), related

    async def prepare(self, category, title, summary, content, assessment_confirmation=None, target_path=None,
                      sources=None, related=None, tags=None, attrs=None):
        if category not in ('entities','projects','concepts','lessons','profile') or any(not isinstance(x, str) for x in (title,summary,content)):
            raise ConnectorError('INVALID_INPUT')
        if not title.strip() or len(title)>120 or len(summary)>500 or not content.strip() or len(content)>8000:
            raise ConnectorError('INVALID_INPUT')
        if assessment_confirmation is not None and (not isinstance(assessment_confirmation, str) or not re.fullmatch('[0-9a-f]{64}', assessment_confirmation)):
            raise ConnectorError('INVALID_INPUT')
        for name, values in (('sources', sources), ('related', related)):
            if values is not None and (not isinstance(values, list) or len(values) > 20 or
                                       any(not isinstance(value, str) or len(value) > 512 for value in values)):
                raise ConnectorError('INVALID_INPUT')
        try:
            tags_value, attrs_value = page_meta.normalize(tags, attrs)
        except ValueError:
            raise ConnectorError('INVALID_INPUT') from None
        replace = None
        if target_path is not None:
            # Editing an existing page: read it first so the preview shows the exact replacement
            # and the caller can be told what will be overwritten.
            if _is_protected(target_path):
                raise ConnectorError('PROTECTED_TARGET')
            replace = await self._existing(target_path)
        incoming_body, _, _ = page_meta.split(content)
        old_body, old_tags, old_attrs = page_meta.split(replace['content']) if replace is not None else ('', [], {})
        final_tags = old_tags if tags is None else tags_value
        final_attrs = old_attrs if attrs is None else attrs_value
        final_content = page_meta.with_meta(incoming_body, final_tags, final_attrs)
        if not final_content.strip() or len(final_content) > 8000:
            raise ConnectorError('INVALID_INPUT')
        async with self.lock:
            self.proposals = {k:v for k,v in self.proposals.items() if self.clock() < v['expires']}
            if len(self.proposals) >= 64:
                raise ConnectorError('PROPOSAL_LIMIT')
            account = await self.session.identity()
            epoch = self.session.epoch
            assessment, related_pages = await self._assessment(category, title, summary, final_content)
            checked_account = await self.session.identity()
            if epoch != self.session.epoch or account != checked_account:
                raise ConnectorError('ACCOUNT_CHANGED')
            identifier = secrets.token_hex(16)
            previous_view = _front_matter_view(replace.get('front_matter')) if replace is not None else None
            if replace is not None:
                path = replace['path']
                operation = 'replace'
            else:
                path = f'wiki/{category}/lingxi-connector-{identifier}'
                operation = 'create'
            front_sources = list(sources) if sources is not None else (previous_view['sources'] if previous_view else [])
            front_related = list(related) if related is not None else (previous_view['related'] if previous_view else [])
            payload = {'path':path, 'front_matter':{'title':title,'summary':summary,'sources':front_sources,'related':front_related}, 'content':final_content}
            review_key = digest({'draft': {'category':category,'title':title,'summary':summary,'content':final_content},
                                 'assessment':assessment, 'account_tag':digest(account), 'epoch':epoch})
            reviewed = assessment_confirmation == review_key
            proposal = {'payload':payload, 'sha256':digest(payload), 'account_tag':digest(account), 'epoch':epoch, 'expires':self.clock()+600,
                        'assessment':assessment, 'assessment_reviewed':reviewed, 'operation':operation}
            if replace is not None:
                proposal['previous'] = {'front_matter': previous_view,
                                        'content_sha256': hashlib.sha256(replace['content'].encode('utf-8')).hexdigest()}
            self.proposals[identifier] = proposal
            self._save(identifier, 'proposal', proposal)
            enabled = self.session.write_enabled_for(account)
            reason = 'UPSTREAM_CREATE_UNPROVEN' if not enabled else 'ASSESSMENT_REVIEW_REQUIRED' if assessment['needs_confirmation'] and not reviewed else None
            preview = {'proposal_id':identifier, 'preview':payload, 'sha256':proposal['sha256'], 'expires_in_seconds':600, 'related_existing':related_pages,
                    'assessment':assessment, 'assessment_review_key':review_key, 'assessment_reviewed':reviewed,
                    'requires_user_authorization':True, 'write_enabled':enabled, 'blocked_reason':reason,
                    'operation': operation, 'tags': final_tags, 'attrs': final_attrs}
            if replace is not None:
                preview['replacing'] = {'path': path,
                                        'previous_title': previous_view['title'],
                                        'previous_content_chars': len(replace['content']),
                                        'previous_content': replace['content'],
                                        'previous_front_matter': previous_view,
                                        'next_front_matter': payload['front_matter'],
                                        'previous_tags': old_tags, 'previous_attrs': old_attrs,
                                        'next_tags': final_tags, 'next_attrs': final_attrs}
            return copy.deepcopy(preview)

    async def prepare_archive(self, target_path, superseded_by=None):
        """Preview permanent deletion of one page; nothing is written here.

        Deleting is only allowed when current evidence shows the page is *already superseded*:
        either the caller names a strict project record that lists this page, or the page itself is
        a strict test/history record, or a search finds a strict project record that lists it.
        The evidence is pinned by path and content fingerprint and re-verified at commit time.
        """
        if _is_protected(target_path):
            raise ConnectorError('PROTECTED_TARGET')
        page = await self._existing(target_path)
        path = page['path']
        if _is_protected(path):
            raise ConnectorError('PROTECTED_TARGET')
        target_record = _strict_record(page.get('content'))
        project_key = target_record[1] if target_record is not None else None
        if superseded_by is not None:
            evidence = await self._explicit_supersession(path, superseded_by, project_key)
        else:
            evidence = await self._superseded_evidence(path, page, project_key)
        if evidence is None:
            raise ConnectorError('TARGET_NOT_SUPERSEDED')
        evidence = dict(evidence, project_key=project_key)
        async with self.lock:
            self.proposals = {k:v for k,v in self.proposals.items() if self.clock() < v['expires']}
            if len(self.proposals) >= 64:
                raise ConnectorError('PROPOSAL_LIMIT')
            account = await self.session.identity()
            epoch = self.session.epoch
            identifier = secrets.token_hex(16)
            content = page['content']
            proposal = {'payload': {'path': path}, 'sha256': digest({'path': path}),
                        'account_tag': digest(account), 'epoch': epoch, 'expires': self.clock()+600,
                        'assessment': {'state': 'not_applicable', 'needs_confirmation': False},
                        'assessment_reviewed': False, 'operation': 'delete',
                        'previous': {'front_matter': _front_matter_view(page.get('front_matter')),
                                     'content': content,
                                     'content_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()},
                        'evidence': evidence}
            self.proposals[identifier] = proposal
            self._save(identifier, 'proposal', proposal)
            enabled = self.session.write_enabled_for(account)
            return copy.deepcopy({
                'proposal_id': identifier, 'operation': 'delete',
                'path': path, 'sha256': proposal['sha256'], 'expires_in_seconds': 600,
                'requires_user_authorization': True, 'write_enabled': enabled,
                'blocked_reason': None if enabled else 'UPSTREAM_CREATE_UNPROVEN',
                'evidence': evidence,
                'deleting': {'path': path,
                             'title': (page.get('front_matter') or {}).get('title'),
                             'content_chars': len(content),
                             'content': content,
                             'as_of': _record_as_of(content)} })

    async def _superseded_evidence(self, path, page, project_key):
        """Return strict evidence that `path` may be deleted, or None."""
        target_record = _strict_record(page.get('content'))
        if target_record is not None:
            target_kind = target_record[0].get('record_kind')
            if target_kind in (RECORD_KIND_TEST, RECORD_KIND_HISTORY):
                return {'kind': 'test_record' if target_kind == RECORD_KIND_TEST else 'history_record',
                        'reason': '本页自身声明为 %s 用途记录。' % target_kind,
                        'content_sha256': hashlib.sha256((page.get('content') or '').encode('utf-8')).hexdigest()}
        try:
            matches = await self.session.search(path.rsplit('/', 1)[-1], 20)
        except ConnectorError:
            return None
        if not isinstance(matches, dict):
            return None
        rows = [row for row in (matches.get('results') or []) if isinstance(row, dict)]
        for row in rows:
            candidate = row.get('path')
            if not isinstance(candidate, str) or candidate == path or not candidate.startswith(PROJECT_PATH_PREFIX):
                continue
            try:
                other = await self._existing(candidate)
            except ConnectorError:
                continue
            if _strict_evidence(other.get('content'), path, project_key) is None:
                continue
            return {'kind': 'superseded_by', 'by': candidate,
                    'reason': '该页已被另一条项目记录明确替代。',
                    'by_content_sha256': hashlib.sha256((other.get('content') or '').encode('utf-8')).hexdigest()}
        return None

    async def _explicit_supersession(self, path, superseding_path, project_key):
        """Verify caller-supplied evidence: a strict project record listing `path`."""
        if not isinstance(superseding_path, str) or superseding_path == path:
            raise ConnectorError('INVALID_INPUT')
        if _is_protected(superseding_path):
            raise ConnectorError('PROTECTED_TARGET')
        if not superseding_path.startswith(PROJECT_PATH_PREFIX):
            raise ConnectorError('INVALID_INPUT')
        other = await self._existing(superseding_path)
        if _strict_evidence(other.get('content'), path, project_key) is None:
            return None
        return {'kind': 'superseded_by', 'by': superseding_path,
                'reason': '该页已被另一条项目记录明确替代。',
                'by_content_sha256': hashlib.sha256((other.get('content') or '').encode('utf-8')).hexdigest()}

    async def _existing(self, path):
        """Fully read one cloud page; partial or non-cloud reads are refused."""
        if not isinstance(path, str) or not path.startswith('wiki/'):
            raise ConnectorError('INVALID_INPUT')
        read_url(path)
        page = await self.session.read(path, 0, 20000)
        if not isinstance(page, dict) or page.get('path') != path:
            raise ConnectorError('UPSTREAM_INCOMPATIBLE')
        if page.get('source') != 'lingxi_cloud':
            raise ConnectorError('UPSTREAM_INCOMPATIBLE')
        if page.get('truncated') is True or page.get('content_scope') != 'full':
            # Never base a replacement on a partial read: the omitted tail would be lost.
            raise ConnectorError('TARGET_READ_INCOMPLETE')
        return page

    def _result(self, identifier, proposal, state, error=None):
        result = {'proposal_id':identifier,'path':proposal['payload']['path'],'sha256':proposal['sha256'],'state':state,'source':'lingxi_cloud' if state=='saved_verified' else None,'error':error}
        self._save(identifier,'receipt',result)
        self.proposals.pop(identifier, None)
        return result

    async def _verify(self, identifier, proposal):
        page = await self.session.read(proposal['payload']['path'], 0, 20000)
        expected = proposal['payload']
        matched = (page.get('source')=='lingxi_cloud' and page.get('path')==expected['path'] and page.get('content_scope')=='full' and page.get('truncated') is False and page.get('content')==expected['content'] and all(page.get('front_matter',{}).get(k)==v for k,v in expected['front_matter'].items()))
        return self._result(identifier,proposal,'saved_verified' if matched else 'verification_failed',None if matched else 'READBACK_MISMATCH')

    async def commit(self, proposal_id):
        async with self.lock:
            existing = self._load(proposal_id,'receipt')
            if existing is not None:
                return existing
            proposal = self.proposals.get(proposal_id)
            if proposal is None:
                raise ConnectorError('PROPOSAL_INVALID_OR_RESTARTED')
            if self.clock() >= proposal['expires']:
                return self._result(proposal_id,proposal,'rejected','PROPOSAL_EXPIRED')
            account = await self.session.identity()
            if proposal['epoch'] != self.session.epoch or proposal['account_tag'] != digest(account):
                return self._result(proposal_id,proposal,'rejected','ACCOUNT_CHANGED')
            if not self.session.write_enabled_for(account):
                return self._result(proposal_id,proposal,'blocked','UPSTREAM_CREATE_UNPROVEN')
            if digest(proposal['payload']) != proposal['sha256']:
                return self._result(proposal_id,proposal,'rejected','PREVIEW_CHANGED')
            if proposal.get('assessment', {}).get('needs_confirmation') and not proposal.get('assessment_reviewed'):
                return self._result(proposal_id,proposal,'blocked','ASSESSMENT_REVIEW_REQUIRED')
            operation = proposal.get('operation', 'create')
            if operation == 'delete':
                return await self._commit_delete(proposal_id, proposal, account)
            target = proposal['payload'].get('path', '')
            if operation == 'create':
                try:
                    await self.session.read(target, 0, 20000)
                except ConnectorError as e:
                    if e.code != 'PAGE_NOT_FOUND':
                        raise
                else:
                    return self._result(proposal_id,proposal,'rejected','TARGET_EXISTS')
                blocked = await self._validate_before_write(proposal_id, proposal)
                if blocked is not None:
                    return blocked
                return await self._write_once(proposal_id, proposal, account, 'create')
            # Replace: existence check, then the project gate, then one final full read
            # immediately before PUT so the checked state is as close to the write as possible.
            try:
                await self.session.read(target, 0, 20000)
            except ConnectorError as e:
                if e.code == 'PAGE_NOT_FOUND':
                    return self._result(proposal_id,proposal,'rejected','TARGET_MISSING')
                raise
            blocked = await self._validate_before_write(proposal_id, proposal)
            if blocked is not None:
                return blocked
            try:
                final = await self.session.read(target, 0, 20000)
            except ConnectorError:
                return self._result(proposal_id,proposal,'rejected','TARGET_UNREADABLE')
            if (not isinstance(final, dict) or final.get('path') != target or
                    final.get('content_scope') != 'full' or final.get('truncated') is True):
                return self._result(proposal_id,proposal,'rejected','TARGET_UNREADABLE')
            previous = proposal.get('previous') or {}
            final_sha = hashlib.sha256((final.get('content') or '').encode('utf-8')).hexdigest()
            if final_sha != previous.get('content_sha256'):
                # Someone edited the page after the preview; overwriting would discard their work.
                return self._result(proposal_id,proposal,'rejected','TARGET_CHANGED')
            if _front_matter_view(final.get('front_matter')) != previous.get('front_matter'):
                return self._result(proposal_id,proposal,'rejected','TARGET_METADATA_CHANGED')
            return await self._write_once(proposal_id, proposal, account, 'replace')

    async def _commit_delete(self, proposal_id, proposal, account):
        path = proposal['payload']['path']
        if _is_protected(path):
            return self._result(proposal_id,proposal,'rejected','PROTECTED_TARGET')
        previous = proposal.get('previous') or {}
        try:
            current = await self.session.read(path, 0, 20000)
        except ConnectorError as e:
            if e.code != 'PAGE_NOT_FOUND':
                raise
            return self._result(proposal_id,proposal,'rejected','TARGET_MISSING')
        current_sha = hashlib.sha256((current.get('content') or '').encode('utf-8')).hexdigest()
        if current_sha != previous.get('content_sha256'):
            return self._result(proposal_id,proposal,'rejected','TARGET_CHANGED')
        evidence_error = await self._evidence_error(proposal)
        if evidence_error is not None:
            return self._result(proposal_id,proposal,'rejected',evidence_error)
        self._result(proposal_id,proposal,'unknown','SUBMISSION_STARTED')
        try:
            await self.session.delete_page(path, account, proposal['epoch'])
        except ConnectorError as e:
            return self._result(proposal_id,proposal,'unknown',e.code)
        except Exception:
            return self._result(proposal_id,proposal,'unknown','WRITE_OUTCOME_UNKNOWN')
        try:
            await self.session.read(path, 0, 20000)
        except ConnectorError as e:
            if e.code == 'PAGE_NOT_FOUND':
                return self._result(proposal_id,proposal,'deleted_verified')
            return self._result(proposal_id,proposal,'verification_failed','READBACK_ERROR')
        return self._result(proposal_id,proposal,'verification_failed','STILL_EXISTS')

    async def _evidence_error(self, proposal):
        """Re-adjudicate delete eligibility right before DELETE; None means still allowed."""
        evidence = proposal.get('evidence') or {}
        kind = evidence.get('kind')
        path = proposal['payload']['path']
        if kind == 'superseded_by':
            by = evidence.get('by')
            if not isinstance(by, str):
                return 'EVIDENCE_CHANGED'
            try:
                other = await self._existing(by)
            except ConnectorError as e:
                return 'EVIDENCE_MISSING' if e.code == 'PAGE_NOT_FOUND' else 'EVIDENCE_UNREADABLE'
            current = hashlib.sha256((other.get('content') or '').encode('utf-8')).hexdigest()
            if current != evidence.get('by_content_sha256'):
                return 'EVIDENCE_CHANGED'
            if _strict_evidence(other.get('content'), path, evidence.get('project_key')) is None:
                return 'EVIDENCE_CHANGED'
            return None
        if kind in ('test_record', 'history_record'):
            strict = _strict_record((proposal.get('previous') or {}).get('content'))
            expected = RECORD_KIND_TEST if kind == 'test_record' else RECORD_KIND_HISTORY
            if strict is None or strict[0].get('record_kind') != expected:
                return 'TARGET_NOT_SUPERSEDED'
            return None
        return 'TARGET_NOT_SUPERSEDED'

    async def _validate_before_write(self, proposal_id, proposal):
        target = proposal['payload'].get('path', '')
        category = target.split('/')[1] if target.startswith('wiki/') and len(target.split('/')) > 2 else ''
        if category != 'projects':
            return None
        # A project handoff must pass the same retrieval simulation before the write. The gate is
        # an invariant of this class: a session that cannot validate is not allowed to write a
        # project page at all, rather than silently skipping verification.
        front = proposal['payload'].get('front_matter') or {}
        try:
            validation = await validate_proposal(
                self.session, category, front.get('title') or '',
                front.get('summary') or '', proposal['payload'].get('content') or '',
                path=target,
            )
        except ConnectorError:
            return self._result(proposal_id, proposal, 'blocked', 'VALIDATION_UNAVAILABLE')
        if validation.get('state') == 'not_applicable':
            return self._result(proposal_id, proposal, 'blocked', 'VALIDATION_UNAVAILABLE')
        if validation.get('state') != 'ok':
            return self._result(proposal_id, proposal, 'blocked', 'VALIDATION_FAILED')
        return None

    async def _write_once(self, proposal_id, proposal, account, operation):
        # Durable state precedes the write; crashes can only be reconciled with GET.
        self._result(proposal_id,proposal,'unknown','SUBMISSION_STARTED')
        try:
            if operation == 'replace':
                await self.session.replace_page(proposal['payload'], account, proposal['epoch'])
            else:
                await self.session.create_page(proposal['payload'], account, proposal['epoch'])
            return await self._verify(proposal_id,proposal)
        except ConnectorError as e:
            return self._result(proposal_id,proposal,'unknown',e.code)
        except Exception:
            return self._result(proposal_id,proposal,'unknown','WRITE_OUTCOME_UNKNOWN')

    async def _verify_deleted(self, proposal_id, proposal):
        """Read-only reconciliation of a delete whose outcome is unknown."""
        path = proposal['payload']['path']
        try:
            if proposal['epoch'] != self.session.epoch:
                raise ConnectorError('ACCOUNT_CHANGED')
            if proposal['account_tag'] != digest(await self.session.identity()):
                raise ConnectorError('ACCOUNT_CHANGED')
            try:
                page = await self.session.read(path, 0, 20000)
            except ConnectorError as e:
                if e.code == 'PAGE_NOT_FOUND':
                    return self._result(proposal_id, proposal, 'deleted_verified')
                raise
            if not isinstance(page, dict) or page.get('path') != path:
                return {'still_exists': True}
            return {'still_exists': True}
        except ConnectorError:
            return None

    async def receipt(self, proposal_id):
        async with self.lock:
            receipt = self._load(proposal_id,'receipt')
            if receipt is None:
                raise ConnectorError('RECEIPT_NOT_FOUND')
            if receipt['state'] != 'unknown':
                return receipt
            proposal = self._load(proposal_id,'proposal')
            if not proposal:
                return receipt
            if proposal['account_tag'] != digest(await self.session.identity()):
                raise ConnectorError('ACCOUNT_CHANGED')
            if proposal.get('epoch') != self.session.epoch:
                raise ConnectorError('ACCOUNT_CHANGED')
            try:
                if proposal.get('operation') == 'delete':
                    outcome = await self._verify_deleted(proposal_id, proposal)
                    if outcome is None:
                        return receipt
                    if outcome.get('still_exists'):
                        return {**receipt, 'error': 'TARGET_STILL_EXISTS'}
                    return outcome
                return await self._verify(proposal_id,proposal)
            except ConnectorError:
                return receipt
