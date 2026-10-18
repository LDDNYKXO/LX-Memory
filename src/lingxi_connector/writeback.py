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

    async def prepare(self, category, title, summary, content, assessment_confirmation=None):
        if category not in ('entities','projects','concepts','lessons','profile') or any(not isinstance(x, str) for x in (title,summary,content)):
            raise ConnectorError('INVALID_INPUT')
        if not title.strip() or len(title)>120 or len(summary)>500 or not content.strip() or len(content)>8000:
            raise ConnectorError('INVALID_INPUT')
        if assessment_confirmation is not None and (not isinstance(assessment_confirmation, str) or not re.fullmatch('[0-9a-f]{64}', assessment_confirmation)):
            raise ConnectorError('INVALID_INPUT')
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
            payload = {'path':f'wiki/{category}/lingxi-connector-{identifier}', 'front_matter':{'title':title,'summary':summary,'sources':[],'related':[]}, 'content':content}
            review_key = digest({'draft': {'category':category,'title':title,'summary':summary,'content':content},
                                 'assessment':assessment, 'account_tag':digest(account), 'epoch':epoch})
            reviewed = assessment_confirmation == review_key
            proposal = {'payload':payload, 'sha256':digest(payload), 'account_tag':digest(account), 'epoch':epoch, 'expires':self.clock()+600,
                        'assessment':assessment, 'assessment_reviewed':reviewed}
            self.proposals[identifier] = proposal
            self._save(identifier, 'proposal', proposal)
            enabled = self.session.write_enabled_for(account)
            reason = 'UPSTREAM_CREATE_UNPROVEN' if not enabled else 'ASSESSMENT_REVIEW_REQUIRED' if assessment['needs_confirmation'] and not reviewed else None
            return copy.deepcopy({'proposal_id':identifier, 'preview':payload, 'sha256':proposal['sha256'], 'expires_in_seconds':600, 'related_existing':related,
                    'assessment':assessment, 'assessment_review_key':review_key, 'assessment_reviewed':reviewed,
                    'requires_user_authorization':True, 'write_enabled':enabled, 'blocked_reason':reason})

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
            try:
                await self.session.read(proposal['payload']['path'],0,20000)
            except ConnectorError as e:
                if e.code != 'PAGE_NOT_FOUND':
                    raise
            else:
                return self._result(proposal_id,proposal,'rejected','TARGET_EXISTS')
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
