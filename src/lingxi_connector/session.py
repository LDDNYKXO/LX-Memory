import asyncio
import json
import shutil
import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path
from .cloud import ConnectorError, read_url, search_url, decode_response, read_result, search_result, IDENTITY_URL, identity_result, PREFIX

class Session:
    supports_validation = True

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.context = None
        self.playwright = None
        self.state = 'NOT_LOGGED_IN'
        self.last_verified_at = None
        self.last_error = None
        self.lock = asyncio.Lock()
        self.epoch = 0
        self.remember = (self.directory / 'remember.json').exists()
        if self.remember:
            self.state = 'UNKNOWN'
        self.monitor = None
        # No client-supplied argument can enable an unverified upstream contract.
        self.create_contract_verified = False
        self.create_contract_account_tag = None
        proof_path = self.directory / 'create-contract.json'
        if proof_path.exists() and not proof_path.is_symlink():
            try:
                proof = json.loads(proof_path.read_text(encoding='utf-8'))
                flags = ('contract_verified','identity_checked','first_readback_match','duplicate_rejected','second_readback_match')
                tag = proof.get('account_tag')
                if all(proof.get(k) is True for k in flags) and proof.get('origin') == PREFIX and proof.get('duplicate_response',{}).get('http_status') == 409 and isinstance(tag,str) and len(tag)==64 and all(c in '0123456789abcdef' for c in tag):
                    self.create_contract_verified = True
                    self.create_contract_account_tag = tag
            except (ValueError, OSError, AttributeError):
                pass

    def write_enabled_for(self, account):
        if not isinstance(account,str):return False
        tag=hashlib.sha256(account.encode()).hexdigest()
        if self.create_contract_verified and tag==self.create_contract_account_tag:return True
        for path in (self.directory/f'create-contract-{tag}.json',self.directory/'create-contract.json'):
            if not path.exists() or path.is_symlink():continue
            try:
                proof=json.loads(path.read_text(encoding='utf-8'))
                flags=('contract_verified','identity_checked','first_readback_match','duplicate_rejected','second_readback_match')
                if all(proof.get(k) is True for k in flags) and proof.get('origin')==PREFIX and proof.get('duplicate_response',{}).get('http_status')==409 and proof.get('account_tag')==tag:
                    self.create_contract_verified=True;self.create_contract_account_tag=tag
                    return True
            except (ValueError,OSError,AttributeError):pass
        return False

    async def verify_create_contract(self):
        """Installer-only, explicitly opted-in validation of one new test page."""
        account = await self.identity()
        if self.write_enabled_for(account):
            return {'contract_verified':True,'reused':True,'post_attempts':0}
        account_tag=hashlib.sha256(account.encode()).hexdigest()
        attempt_file=self.directory/f'last-create-attempt-{account_tag}.json'
        legacy_attempt=self.directory/'last-create-attempt.json'
        if not attempt_file.exists() and legacy_attempt.exists() and not legacy_attempt.is_symlink():
            try:legacy=json.loads(legacy_attempt.read_text(encoding='utf-8'))
            except (ValueError,OSError):raise ConnectorError('INVALID_RECEIPT') from None
            if isinstance(legacy,dict) and legacy.get('account_tag')==account_tag:
                attempt_file.write_text(json.dumps(legacy,ensure_ascii=False,indent=2),encoding='utf-8')
        if attempt_file.exists():
            if attempt_file.is_symlink():raise ConnectorError('INVALID_RECEIPT')
            try:previous=json.loads(attempt_file.read_text(encoding='utf-8'))
            except (ValueError,OSError):raise ConnectorError('INVALID_RECEIPT') from None
            if not isinstance(previous,dict):raise ConnectorError('INVALID_RECEIPT')
            if previous.get('account_tag')==account_tag and previous.get('post_attempts',0)>0 and not previous.get('contract_verified'):
                previous.pop('account_tag',None)
                return {**previous,'error':previous.get('error') or 'PROBE_OUTCOME_UNKNOWN','reused':True,'requires_read_only_recovery':True}
        identifier=secrets.token_hex(16)
        payload={'path':f'wiki/concepts/lingxi-connector-check-{identifier}','front_matter':{'title':'灵犀记忆接入校验','summary':'安装时经本人选择的保存功能校验','sources':[],'related':[]},'content':'本页仅用于验证灵犀记忆接入产品可以新增并读回记忆，重复创建应被拒绝；不代表任何业务项目完成。'}
        result={'origin':PREFIX,'path':payload['path'],'identity_checked':True,'first_readback_match':False,'duplicate_rejected':False,'second_readback_match':False,'contract_verified':False,'post_attempts':0}
        async with self.lock:
            epoch=self.epoch
            def checkpoint():
                temporary=attempt_file.with_suffix('.tmp')
                if temporary.is_symlink() or attempt_file.is_symlink():raise ConnectorError('INVALID_RECEIPT')
                temporary.write_text(json.dumps({**result,'account_tag':account_tag},ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(attempt_file)
            async def identity_now():
                response=await self.context.request.get(IDENTITY_URL,timeout=20000,max_redirects=0)
                try:
                    if response.url!=IDENTITY_URL:raise ConnectorError('ACCOUNT_CHANGED')
                    value=identity_result(response.status,json.loads(await response.text()))
                finally:await response.dispose()
                if value!=account or epoch!=self.epoch:raise ConnectorError('ACCOUNT_CHANGED')
            async def page_now():
                response=await self.context.request.get(read_url(payload['path']),timeout=20000,max_redirects=0)
                try:return read_result(decode_response(response.status,response.url,await response.text()),0,20000)
                finally:await response.dispose()
            def match(page):
                return page.get('path')==payload['path'] and page.get('content_scope')=='full' and page.get('truncated') is False and page.get('content')==payload['content'] and all(page.get('front_matter',{}).get(k)==v for k,v in payload['front_matter'].items())
            async def post_once():
                await identity_now()
                if epoch!=self.epoch:raise ConnectorError('ACCOUNT_CHANGED')
                result['post_attempts']+=1
                checkpoint()
                response=await self.context.request.post(PREFIX+'pages',data=payload,timeout=20000,max_redirects=0)
                try:
                    body=json.loads(await response.text())
                    if response.url!=PREFIX+'pages' or not isinstance(body,dict):raise ConnectorError('UPSTREAM_INCOMPATIBLE')
                    return {'http_status':response.status,'result':body.get('result'),'v7code':body.get('v7code')}
                finally:await response.dispose()
            try:
                await identity_now()
                try:await page_now()
                except ConnectorError as e:
                    if e.code!='PAGE_NOT_FOUND':raise
                else:raise ConnectorError('TARGET_EXISTS')
                first=await post_once()
                result['first_response']=first
                if first!={'http_status':200,'result':'ok','v7code':0}:raise ConnectorError('PROBE_OUTCOME_UNKNOWN')
                result['first_readback_match']=match(await page_now())
                if not result['first_readback_match']:raise ConnectorError('READBACK_MISMATCH')
                second=await post_once()
                result['duplicate_response']=second
                result['duplicate_rejected']=second=={'http_status':409,'result':'ResourceConflict','v7code':409433001}
                result['second_readback_match']=match(await page_now())
                await identity_now()
                result['contract_verified']=result['duplicate_rejected'] and result['second_readback_match']
                if result['contract_verified']:
                    tag=hashlib.sha256(account.encode()).hexdigest()
                    proof={**result,'account_tag':tag}
                    target=self.directory/f'create-contract-{account_tag}.json';temporary=target.with_suffix('.tmp')
                    if target.is_symlink() or temporary.is_symlink():raise ConnectorError('INVALID_INPUT')
                    temporary.write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(target)
                    self.create_contract_verified=True;self.create_contract_account_tag=tag
            except ConnectorError as e:result['error']=e.code
            except Exception:result['error']='PROBE_OUTCOME_UNKNOWN'
            if result['post_attempts']:
                try:checkpoint()
                except (OSError,ConnectorError):result['error']='PROBE_OUTCOME_UNKNOWN'
        return result

    async def identity(self):
        async with self.lock:
            if self.context is None:
                if not self.remember:
                    raise ConnectorError('AUTH_REQUIRED')
                await self._launch(False)
            epoch = self.epoch
            try:
                response = await self.context.request.get(IDENTITY_URL, timeout=20000, max_redirects=0)
                try:
                    if response.url != IDENTITY_URL:
                        raise ConnectorError('AUTH_REQUIRED')
                    account = identity_result(response.status, json.loads(await response.text()))
                finally:
                    await response.dispose()
                if epoch != self.epoch:
                    raise ConnectorError('AUTH_REQUIRED')
                return account
            except ConnectorError:
                raise
            except Exception:
                raise ConnectorError('ACCOUNT_UNVERIFIED') from None

    async def create_page(self, payload, expected_account=None, expected_epoch=None):
        if not self.create_contract_verified:
            raise ConnectorError('UPSTREAM_CREATE_UNPROVEN')
        if not self.write_enabled_for(expected_account):
            raise ConnectorError('ACCOUNT_CHANGED')
        if not isinstance(payload, dict) or set(payload) != {'path', 'front_matter', 'content'}:
            raise ConnectorError('INVALID_INPUT')
        read_url(payload['path'])
        parts = payload['path'].split('/')
        if len(parts) != 3 or parts[0] != 'wiki' or parts[1] not in ('entities','projects','concepts','lessons','profile') or not parts[2].startswith('lingxi-connector-'):
            raise ConnectorError('INVALID_INPUT')
        async with self.lock:
            if self.context is None:
                raise ConnectorError('AUTH_REQUIRED')
            epoch = self.epoch
            if expected_epoch != epoch or not isinstance(expected_account, str):
                raise ConnectorError('ACCOUNT_CHANGED')
            try:
                identity = await self.context.request.get(IDENTITY_URL, timeout=20000, max_redirects=0)
                try:
                    if identity.url != IDENTITY_URL or identity_result(identity.status, json.loads(await identity.text())) != expected_account or epoch != self.epoch:
                        raise ConnectorError('ACCOUNT_CHANGED')
                finally:
                    await identity.dispose()
                if epoch != self.epoch:
                    raise ConnectorError('ACCOUNT_CHANGED')
                response = await self.context.request.post(PREFIX + 'pages', data=payload, timeout=20000, max_redirects=0)
                try:
                    result = decode_response(response.status, response.url, await response.text())
                finally:
                    await response.dispose()
                if epoch != self.epoch:
                    raise ConnectorError('AUTH_REQUIRED')
                return result
            except ConnectorError:
                raise
            except Exception:
                raise ConnectorError('WRITE_OUTCOME_UNKNOWN') from None

    async def _launch(self, headed):
        from playwright.async_api import async_playwright
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.directory / 'profile'), channel='msedge', headless=not headed,
                accept_downloads=False, timeout=30000)
            owned = self.context
            owned.on('close', lambda *args: self.context_closed(owned))
        except Exception:
            self.last_error = 'BROWSER_START_FAILED'
            raise ConnectorError('BROKER_UNAVAILABLE', 'Edge启动失败；请安装Microsoft Edge。') from None

    def context_closed(self, owned):
        if self.context is owned:
            self.context = None
            self.state = 'UNKNOWN' if self.remember else 'AUTH_REQUIRED'

    async def login(self, remember=False):
        if type(remember) is not bool:
            raise ConnectorError('INVALID_INPUT')
        async with self.lock:
            if self.context:
                await self.context.close()
                self.context = None
            self.epoch += 1
            self.remember = remember
            if remember:
                (self.directory / 'remember.json').write_text('{"consent":true}', encoding='utf-8')
            else:
                (self.directory / 'remember.json').unlink(missing_ok=True)
            await self._launch(True)
            page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await page.goto('https://lingxi.kdocs.cn/', wait_until='domcontentloaded', timeout=45000)
            self.state = 'LOGIN_IN_PROGRESS'
            self.last_error = None
            if self.monitor:
                self.monitor.cancel()
            self.monitor = asyncio.create_task(self._monitor_login(self.epoch))
            return await self.status()

    async def _monitor_login(self, epoch):
        # Bounded polling of one GET, no clicks/terms/captcha automation.
        for _ in range(120):
            await asyncio.sleep(5)
            if epoch != self.epoch or self.context is None:
                return
            try:
                await self.search('记忆', 1)
                return
            except ConnectorError as e:
                self.last_error = e.code
                if e.code in ('PERMISSION_DENIED', 'RATE_LIMITED'):
                    return
                if self.state != 'CONNECTED':
                    self.state = 'LOGIN_IN_PROGRESS'
        self.state = 'AUTH_REQUIRED'

    async def status(self, refresh=False):
        state = self.state
        if state == 'CONNECTED' and self.last_verified_at:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(self.last_verified_at)).total_seconds()
            if age > 300:
                state = 'UNKNOWN'
        if refresh and self.remember and state == 'UNKNOWN':
            try:
                await self.search('记忆', 1)
            except ConnectorError:
                pass
            state = self.state
        return {'state': state, 'cloud_available': state == 'CONNECTED',
                'last_verified_at': self.last_verified_at, 'error': self.last_error,
                'remember_login': self.remember}

    async def _get(self, url):
        async with self.lock:
            if self.context is None:
                if not self.remember:
                    raise ConnectorError('AUTH_REQUIRED')
                await self._launch(False)
            epoch = self.epoch
            try:
                response = await self.context.request.get(url, timeout=20000, max_redirects=0)
                try:
                    data = decode_response(response.status, response.url, await response.text())
                finally:
                    await response.dispose()
                if epoch != self.epoch:
                    raise ConnectorError('AUTH_REQUIRED')
                self.last_verified_at = datetime.now(timezone.utc).isoformat()
                self.state = 'CONNECTED'
                self.last_error = None
                return data
            except ConnectorError as e:
                self.last_error = e.code
                if e.code == 'AUTH_REQUIRED':
                    self.state = 'AUTH_REQUIRED'
                elif e.code == 'NETWORK_ERROR':
                    self.state = 'NETWORK_ERROR'
                else:
                    self.state = 'UNKNOWN'
                raise
            except Exception:
                self.state = 'NETWORK_ERROR'
                self.last_error = 'NETWORK_ERROR'
                raise ConnectorError('NETWORK_ERROR') from None

    async def search(self, query, limit=5):
        return search_result(await self._get(search_url(query, limit)))

    async def read(self, path, offset=0, max_chars=8000):
        # Validate paging before any network access.
        if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= 20000:
            raise ConnectorError('INVALID_INPUT')
        data = await self._get(read_url(path))
        if data.get('path') != path:
            raise ConnectorError('UPSTREAM_INCOMPATIBLE')
        return read_result(data, offset, max_chars)

    async def logout(self):
        # Advance before waiting for lock: responses from prior epoch are discarded.
        self.epoch += 1
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None
        async with self.lock:
            if self.context:
                await self.context.close()
                self.context = None
            profile = self.directory / 'profile'
            if profile.exists():
                if profile.is_symlink() or profile.resolve().parent != self.directory:
                    raise ConnectorError('BROKER_UNAVAILABLE')
                shutil.rmtree(profile)
            (self.directory / 'remember.json').unlink(missing_ok=True)
            self.remember = False
            self.state = 'NOT_LOGGED_IN'
            self.last_error = None
            self.last_verified_at = None
            return await self.status()

    async def close(self):
        if self.monitor:
            self.monitor.cancel()
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        if not self.remember:
            await self.logout()
