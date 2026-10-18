"""Short-lived previews and at-most-once submission; never infer user consent."""
import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from .cloud import ConnectorError, read_url
from .prepare_assessment import assess
from .proposal_validation import validate_proposal

ASSESSMENT_TIMEOUT = 30
ASSESSMENT_READ_CHARS = 12000

# Scopes that must never be deleted or replaced by this product, whatever the caller asks.
PROTECTED_PREFIXES = ('core/', 'wiki/profile/')

def _is_protected(path):
    return not isinstance(path, str) or any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES)

def _record_marker(content):
    """Parse the project record marker of a page, or None when it has none."""
    if not isinstance(content, str):
        return None
    stripped = content.lstrip()
    first = stripped.splitlines()[0] if stripped.splitlines() else ''
    prefix = '<!-- memory-project-record '
    if not first.startswith(prefix) or not first.endswith(' -->'):
        return None
    try:
        value = json.loads(first[len(prefix):-4])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None

def _record_as_of(content):
    marker = _record_marker(content)
    as_of = marker.get('as_of') if marker else None
    return as_of if isinstance(as_of, str) else None

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

    async def prepare(self, category, title, summary, content, assessment_confirmation=None, target_path=None):
        if category not in ('entities','projects','concepts','lessons','profile') or any(not isinstance(x, str) for x in (title,summary,content)):
            raise ConnectorError('INVALID_INPUT')
        if not title.strip() or len(title)>120 or len(summary)>500 or not content.strip() or len(content)>8000:
            raise ConnectorError('INVALID_INPUT')
        if assessment_confirmation is not None and (not isinstance(assessment_confirmation, str) or not re.fullmatch('[0-9a-f]{64}', assessment_confirmation)):
            raise ConnectorError('INVALID_INPUT')
        replace = None
        if target_path is not None:
            # Editing an existing page: read it first so the preview shows the exact replacement
            # and the caller can be told what will be overwritten.
            if _is_protected(target_path):
                raise ConnectorError('PROTECTED_TARGET')
            replace = await self._existing(target_path)
        async with self.lock:
            self.proposals = {k:v for k,v in self.proposals.items() if self.clock() < v['expires']}
            if len(self.proposals) >= 64:
                raise ConnectorError('PROPOSAL_LIMIT')
            account = await self.session.identity()
            epoch = self.session.epoch
            assessment, related = await self._assessment(category, title, summary, content)
            checked_account = await self.session.identity()
            if epoch != self.session.epoch or account != checked_account:
                raise ConnectorError('ACCOUNT_CHANGED')
            identifier = secrets.token_hex(16)
            if replace is not None:
                path = replace['path']
                operation = 'replace'
            else:
                path = f'wiki/{category}/lingxi-connector-{identifier}'
                operation = 'create'
            payload = {'path':path, 'front_matter':{'title':title,'summary':summary,'sources':[],'related':[]}, 'content':content}
            review_key = digest({'draft': {'category':category,'title':title,'summary':summary,'content':content},
                                 'assessment':assessment, 'account_tag':digest(account), 'epoch':epoch})
            reviewed = assessment_confirmation == review_key
            proposal = {'payload':payload, 'sha256':digest(payload), 'account_tag':digest(account), 'epoch':epoch, 'expires':self.clock()+600,
                        'assessment':assessment, 'assessment_reviewed':reviewed, 'operation':operation}
            if replace is not None:
                proposal['previous'] = {'front_matter': replace.get('front_matter'),
                                        'content_sha256': hashlib.sha256(replace['content'].encode('utf-8')).hexdigest()}
            self.proposals[identifier] = proposal
            self._save(identifier, 'proposal', proposal)
            enabled = self.session.write_enabled_for(account)
            reason = 'UPSTREAM_CREATE_UNPROVEN' if not enabled else 'ASSESSMENT_REVIEW_REQUIRED' if assessment['needs_confirmation'] and not reviewed else None
            preview = {'proposal_id':identifier, 'preview':payload, 'sha256':proposal['sha256'], 'expires_in_seconds':600, 'related_existing':related,
                    'assessment':assessment, 'assessment_review_key':review_key, 'assessment_reviewed':reviewed,
                    'requires_user_authorization':True, 'write_enabled':enabled, 'blocked_reason':reason,
                    'operation': operation}
            if replace is not None:
                preview['replacing'] = {'path': path,
                                        'previous_title': (replace.get('front_matter') or {}).get('title'),
                                        'previous_content_chars': len(replace['content']),
                                        'previous_content': replace['content']}
            return copy.deepcopy(preview)

    async def prepare_archive(self, target_path, superseded_by=None):
        """Preview permanent deletion of one page; nothing is written here.

        Deleting is only allowed when current evidence shows the page is *already superseded*:
        either the caller names the superseding page explicitly (the connector reads it back and
        checks that this page is in its `supersedes` list), or the page itself declares a
        `record_kind` of test/history, or a search finds another page that supersedes it. A caller
        cannot delete an arbitrary page just because it typed its path.
        """
        if _is_protected(target_path):
            raise ConnectorError('PROTECTED_TARGET')
        page = await self._existing(target_path)
        path = page['path']
        if _is_protected(path):
            raise ConnectorError('PROTECTED_TARGET')
        if superseded_by is not None:
            evidence = await self._explicit_supersession(path, superseded_by)
        else:
            evidence = await self._superseded_evidence(path, page)
        if evidence is None:
            raise ConnectorError('TARGET_NOT_SUPERSEDED')
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
                        'previous': {'front_matter': page.get('front_matter'),
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

    async def _superseded_evidence(self, path, page):
        """Return why `path` is safe to delete, or None when current evidence does not say so."""
        marker = _record_marker(page.get('content'))
        if marker is not None and marker.get('record_kind') == 'test':
            return {'kind': 'test_record', 'reason': '本页自身声明为 test 用途记录。'}
        if marker is not None and marker.get('record_kind') == 'history':
            return {'kind': 'history_record', 'reason': '本页自身声明为 history 用途记录。'}
        try:
            matches = await self.session.search(path.rsplit('/', 1)[-1], 20)
        except ConnectorError:
            return None
        if not isinstance(matches, dict):
            return None
        rows = [row for row in (matches.get('results') or []) if isinstance(row, dict)]
        for row in rows:
            candidate = row.get('path')
            if not isinstance(candidate, str) or candidate == path or not candidate.startswith('wiki/'):
                continue
            try:
                other = await self.session.read(candidate, 0, 20000)
            except ConnectorError:
                continue
            if not isinstance(other, dict) or other.get('truncated') is True:
                continue
            other_marker = _record_marker(other.get('content'))
            if other_marker is None:
                continue
            if path in (other_marker.get('supersedes') or []):
                return {'kind': 'superseded_by', 'by': candidate,
                        'reason': '该页已被另一条项目记录明确替代。'}
        return None

    async def _explicit_supersession(self, path, superseding_path):
        """Verify caller-supplied evidence: the named page must list `path` in its supersedes."""
        if not isinstance(superseding_path, str) or superseding_path == path:
            raise ConnectorError('INVALID_INPUT')
        if _is_protected(superseding_path):
            raise ConnectorError('PROTECTED_TARGET')
        if not superseding_path.startswith('wiki/'):
            raise ConnectorError('INVALID_INPUT')
        other = await self._existing(superseding_path)
        marker = _record_marker(other.get('content'))
        if marker is None or path not in (marker.get('supersedes') or []):
            return None
        return {'kind': 'superseded_by', 'by': superseding_path,
                'reason': '该页已被另一条项目记录明确替代。'}

    async def _existing(self, path):
        """Read the page that a replace proposal intends to overwrite."""
        if not isinstance(path, str) or not path.startswith('wiki/'):
            raise ConnectorError('INVALID_INPUT')
        read_url(path)
        page = await self.session.read(path, 0, 20000)
        if not isinstance(page, dict) or page.get('path') != path:
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
            try:
                await self.session.read(proposal['payload']['path'],0,20000)
            except ConnectorError as e:
                if e.code != 'PAGE_NOT_FOUND':
                    raise
                if operation == 'replace':
                    # The page was deleted between preview and commit: refuse instead of creating
                    # a new page at a path the user did not approve as a new page.
                    return self._result(proposal_id,proposal,'rejected','TARGET_MISSING')
            else:
                if operation == 'create':
                    return self._result(proposal_id,proposal,'rejected','TARGET_EXISTS')
                previous = proposal.get('previous') or {}
                try:
                    current = await self.session.read(proposal['payload']['path'], 0, 20000)
                except ConnectorError:
                    return self._result(proposal_id,proposal,'rejected','TARGET_UNREADABLE')
                current_sha = hashlib.sha256((current.get('content') or '').encode('utf-8')).hexdigest()
                if current_sha != previous.get('content_sha256'):
                    # Someone edited the page after the preview; overwriting would discard their work.
                    return self._result(proposal_id,proposal,'rejected','TARGET_CHANGED')
            # A project handoff must pass the same retrieval simulation before the POST. The gate is
            # an invariant of this class: a session that cannot validate is not allowed to write a
            # project page at all, rather than silently skipping verification.
            target = proposal['payload'].get('path', '')
            category = target.split('/')[1] if target.startswith('wiki/') and len(target.split('/')) > 2 else ''
            if category == 'projects':
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
            # Durable state precedes POST; crashes can only be reconciled with GET.
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

    async def receipt(self, proposal_id):
        async with self.lock:
            receipt = self._load(proposal_id,'receipt')
            if receipt is None:
                raise ConnectorError('RECEIPT_NOT_FOUND')
            if receipt['state']=='unknown':
                proposal = self._load(proposal_id,'proposal')
                if not proposal:
                    return receipt
                if proposal['account_tag'] != digest(await self.session.identity()):
                    raise ConnectorError('ACCOUNT_CHANGED')
                try:
                    return await self._verify(proposal_id,proposal)
                except ConnectorError:
                    return receipt
            return receipt
