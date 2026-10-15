"""Selected-client configuration with complete preflight and private backups."""
import copy
import hashlib
import json
from pathlib import Path
import re
import secrets
import os
import base64
import subprocess
import tomllib
from .broker import private_directory

CLIENTS=('Codex','DSH','WorkBuddy','ZCode')
KNOWN_SKILLS={'4391b6ad3816807e82f09e4e56268b8bbff55678faf6d04c91da3fadb1247e67','ebedec54920ea4856ee37e83b16709eade1af4f053f78c9d0b83e9061aaf51a3'}
class InstallerError(Exception):pass
def sha(value):return hashlib.sha256(value).hexdigest()
def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise InstallerError('配置有重复键，未修改')
        result[key]=value
    return result
def load_json(raw):return json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique_object)

def definition(root,python):
    return {'command':str(python),'args':['-B',str(root/'run.py'),'mcp'],'env':{'PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'}}

def native_write(target,raw,updated,root):
    if os.name!='nt':raise PermissionError('Native Windows writer unavailable')
    plan=root/'runtime/session'/('config-write-'+secrets.token_hex(12)+'.json')
    plan.write_text(json.dumps({'target':str(target),'before_sha256':sha(raw) if raw is not None else None,'after_sha256':sha(updated),'content_base64':base64.b64encode(updated).decode('ascii')}),encoding='utf-8')
    powershell=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    result=subprocess.run([str(powershell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(root/'write_config.ps1'),'-InputFile',str(plan)],capture_output=True,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode!=0:raise InstallerError('Windows配置写入失败，未调整权限；备份和恢复记录已保留')

def merged(existing,desired,owned_args):
    if existing is None:return desired
    if not isinstance(existing,dict) or existing.get('args') not in owned_args:
        raise InstallerError('已有同名灵犀连接来自其它路径；保留原配置，未替换')
    after={**existing,**desired}
    after['env']={**existing.get('env',{}),**desired['env']}
    return after

def toml_table(parts,value):
    scalar={k:v for k,v in value.items() if not isinstance(v,dict)}
    result='['+'.'.join(json.dumps(p,ensure_ascii=False) for p in parts)+']\n'
    for key,item in scalar.items():
        if not isinstance(item,(str,bool,int,float,list)) or item is None:raise InstallerError('不支持的TOML值')
        result+=json.dumps(key,ensure_ascii=False)+' = '+json.dumps(item,ensure_ascii=False)+'\n'
    for key,item in value.items():
        if isinstance(item,dict):result+='\n'+toml_table(parts+[key],item)
    return result

def codex_bytes(raw,desired,owned_args):
    text=raw.decode('utf-8-sig') if raw is not None else ''
    before=tomllib.loads(text)
    servers=before.get('mcp_servers',{})
    if not isinstance(servers,dict):raise InstallerError('Codex MCP配置格式不明')
    old=servers.get('lingxi-memory')
    entry=merged(old,desired,owned_args)
    if old==entry:return raw
    lines=text.splitlines(keepends=True);output=[];remove=False;found=False
    for line in lines:
        if re.match(r'^\s*\[',line):
            remove=False
            try:
                table=tomllib.loads(line)
                remove='lingxi-memory' in table.get('mcp_servers',{})
            except tomllib.TOMLDecodeError:pass
            found=found or remove
        if not remove:output.append(line)
    if old is not None and not found:raise InstallerError('Codex服务器表格式不支持，未修改')
    generated=''.join(output)+'\n'+toml_table(['mcp_servers','lingxi-memory'],entry)
    after=tomllib.loads(generated)
    check=copy.deepcopy(after);check['mcp_servers'].pop('lingxi-memory')
    baseline=copy.deepcopy(before);baseline.setdefault('mcp_servers',{}).pop('lingxi-memory',None)
    if check!=baseline:raise InstallerError('Codex其它配置发生变化，未写入')
    return (b'\xef\xbb\xbf' if raw and raw.startswith(b'\xef\xbb\xbf') else b'')+generated.encode('utf-8')

def json_bytes(raw,client,desired,owned_args):
    before=load_json(raw) if raw is not None else {}
    if not isinstance(before,dict):raise InstallerError('JSON配置格式不明')
    after=copy.deepcopy(before);cursor=after
    keys=('mcpServers',) if client=='WorkBuddy' else ('mcp','servers')
    for key in keys:
        if key not in cursor:cursor[key]={}
        cursor=cursor[key]
        if not isinstance(cursor,dict):raise InstallerError('MCP配置格式不明')
    old=cursor.get('lingxi-memory')
    entry=merged(old,{'type':'stdio',**desired,**({'enabled':True} if client=='ZCode' else {})},owned_args)
    if old==entry:return raw
    cursor['lingxi-memory']=entry
    return (json.dumps(after,ensure_ascii=False,indent=2)+'\n').encode('utf-8')

def yaml_bytes(raw,desired,root,owned_args):
    import yaml
    class TaggedScalar(str):
        """Opaque passthrough for extension tags such as DSH's !!js expressions."""
    class StrictLoader(yaml.SafeLoader):pass
    def mapping(loader,node,deep=False):
        loader.flatten_mapping(node)
        return unique_object([(loader.construct_object(k,deep=deep),loader.construct_object(v,deep=deep)) for k,v in node.value])
    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,mapping)
    # A DSH patch layer may use !!js expressions (e.g. path: !!js dshHomePath('x.sqlite')).
    # Keep them as opaque text. Updates rewrite only the target entry, so every untouched
    # entry keeps its original bytes and existing expressions are never re-serialized.
    StrictLoader.add_constructor('tag:yaml.org,2002:js',lambda loader,node:TaggedScalar(loader.construct_scalar(node)))
    text=raw.decode('utf-8-sig') if raw is not None else ''
    try:before=yaml.load(text,Loader=StrictLoader) if text.strip() else []
    except yaml.YAMLError as e:raise InstallerError('DSH配置解析失败，未修改：'+((str(e).splitlines() or ['未知原因'])[0])) from e
    if not isinstance(before,list):raise InstallerError('DSH配置格式不明')
    after=copy.deepcopy(before);hits=[]
    def walk(value,index):
        if isinstance(value,dict):
            if value.get('id')=='mcp-lingxi-memory' or (isinstance(value.get('config'),dict) and value['config'].get('serverName')=='lingxi-memory'):
                hits.append((index,value))
            for child in value.values():walk(child,index)
        elif isinstance(value,list):
            for child in value:walk(child,index)
    for index,item in enumerate(after):walk(item,index)
    if len(hits)>1:raise InstallerError('DSH灵犀配置重复，未修改')
    config={'serverName':'lingxi-memory','transport':'stdio',**desired,'cwd':str(root),'toolCallTimeoutMs':60000,'failOnStartupError':False}
    if hits:
        index,item=hits[0]
        if item.get('name')!='@deepseek-ai/dsh-mcp-client':raise InstallerError('DSH同名插件冲突')
        entry=merged(item.get('config'),config,owned_args)
        if item['config']==entry:return raw
        item['config']=entry
        lines=text.splitlines(keepends=True);starts=[i for i,line in enumerate(lines) if re.match(r'^-\s',line)]
        if len(starts)!=len(before):raise InstallerError('DSH配置格式不支持局部更新')
        start=starts[index];end=starts[index+1] if index+1<len(starts) else len(lines)
        result=''.join(lines[:start])+yaml.safe_dump([after[index]],allow_unicode=True,sort_keys=False)+''.join(lines[end:])
    else:
        entry={'insert':[{'id':'mcp-lingxi-memory','name':'@deepseek-ai/dsh-mcp-client','config':config}]}
        after.append(entry)
        result=text+'\n'+yaml.safe_dump([entry],allow_unicode=True,sort_keys=False)
    if yaml.load(result,Loader=StrictLoader)!=after:raise InstallerError('DSH更新校验失败')
    return (b'\xef\xbb\xbf' if raw and raw.startswith(b'\xef\xbb\xbf') else b'')+result.encode('utf-8')

def safe_target(path,home):
    if not path.resolve().is_relative_to(home):raise InstallerError('目标路径超出当前用户目录')
    for candidate in (path,*path.parents):
        if candidate.is_symlink() or (hasattr(candidate,'is_junction') and candidate.is_junction()):raise InstallerError('配置路径是链接，未修改')
        if candidate==home:break

def configure(root,home,python,clients,apply=False):
    root=Path(root).resolve();home=Path(home).resolve();python=Path(python).resolve()
    if not clients or len(set(clients))!=len(clients) or any(c not in CLIENTS for c in clients):raise InstallerError('请选择有效助手')
    desired=definition(root,python);owned_args=[desired['args']]
    owned_roots=[str(root)]
    registry=home/'.lingxi-memory-connector/install.json'
    safe_target(registry,home)
    if registry.exists():
        prior=load_json(registry.read_bytes())
        if prior.get('product')!='lingxi-memory-connector':raise InstallerError('安装登记格式不明')
        roots=prior.get('roots',[prior.get('root')])
        if not isinstance(roots,list) or any(not isinstance(p,str) or not Path(p).is_absolute() for p in roots):raise InstallerError('安装登记路径格式不明')
        for prior_root in roots:
            if prior_root not in owned_roots:owned_roots.append(prior_root)
            owned_args.append(['-B',str(Path(prior_root)/'run.py'),'mcp'])
    paths={'Codex':home/'.codex/config.toml','DSH':home/'.dsh/profiles/desktop/cordis.patch.yml','WorkBuddy':home/'.workbuddy/mcp.json','ZCode':home/'.zcode/cli/config.json'}
    mutations=[];report={'applied':apply,'clients':[],'desktop_native_verified':False}
    try:
        for client in clients:
            target=paths[client];safe_target(target,home)
            raw=target.read_bytes() if target.exists() else None
            if client=='Codex':updated=codex_bytes(raw,desired,owned_args)
            elif client=='DSH':updated=yaml_bytes(raw,desired,root,owned_args)
            else:updated=json_bytes(raw,client,desired,owned_args)
            mutations.append((target,raw,updated))
            report['clients'].append({'client':client,'config_path':str(target),'configured':apply,'changed':raw!=updated,'refresh_required':True})
        skill=(root/'skills/lingxi-cloud-memory/SKILL.md').read_bytes()
        skill_roots=[]
        if any(c in clients for c in ('Codex','DSH','ZCode')):skill_roots.append(home/'.agents/skills')
        if 'WorkBuddy' in clients:skill_roots.append(home/'.workbuddy/skills')
        for skill_root in skill_roots:
            target=skill_root/'lingxi-cloud-memory/SKILL.md';safe_target(target,home)
            raw=target.read_bytes() if target.exists() else None
            if raw is not None and raw!=skill and sha(raw) not in KNOWN_SKILLS:raise InstallerError('技能有用户修改，保留原文件')
            mutations.append((target,raw,skill))
        raw=registry.read_bytes() if registry.exists() else None
        value={'product':'lingxi-memory-connector','root':str(root),'roots':owned_roots,'python':str(python)}
        updated=(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
        if raw is not None and load_json(raw)==value:updated=raw
        mutations.append((registry,raw,updated))
    except InstallerError:raise
    except (ValueError,OSError,TypeError,KeyError) as e:raise InstallerError('配置解析失败，未修改任何目标') from e
    if not apply:return report
    directory=root/'runtime/session';private_directory(directory)
    backups=directory/'setup-backups';backups.mkdir(exist_ok=True)
    transaction=secrets.token_hex(12);done=[];entries=[]
    journal=directory/('setup-'+transaction+'.json')
    def save_journal(finished=False):
        temporary=journal.with_suffix('.tmp')
        with temporary.open('x',encoding='utf-8') as stream:
            json.dump({'entries':entries,'completed':done,'finished':finished},stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        temporary.replace(journal)
    for index,(target,raw,updated) in enumerate(mutations):
        if raw==updated:continue
        if (target.read_bytes() if target.exists() else None)!=raw:raise InstallerError('配置被其它程序改变，请重新运行安装入口')
        safe_target(target,home)
        if raw is not None:(backups/f'{transaction}-{index}.bak').write_bytes(raw)
        entries.append({'path':str(target),'before_sha256':sha(raw) if raw is not None else None,'after_sha256':sha(updated),'backup_file':str(backups/f'{transaction}-{index}.bak') if raw is not None else None,'state':'prepared'})
    save_journal()
    try:
        changed=[m for m in mutations if m[1]!=m[2]]
        for entry,(target,raw,updated) in zip(entries,changed):
            safe_target(target,home)
            if (target.read_bytes() if target.exists() else None)!=raw:raise InstallerError('提交前检测到配置并发变化')
            entry['state']='replacing';save_journal()
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_name(target.name+'.lingxi-'+transaction+'.tmp')
            try:
                with temporary.open('xb') as stream:stream.write(updated)
            except PermissionError:
                # An existing file can be writable although creating a sibling is denied.
                # Never change permissions; retain write-ahead journal and full preimage.
                if raw is None:raise
                entry['write_method']='existing_file';save_journal()
                try:
                    with target.open('r+b') as stream:
                        if stream.read()!=raw:raise InstallerError('提交前检测到配置并发变化')
                        stream.seek(0);stream.write(updated);stream.truncate();stream.flush();os.fsync(stream.fileno())
                except PermissionError:
                    entry['write_method']='windows_native';save_journal()
                    native_write(target,raw,updated,root)
            else:
                if (target.read_bytes() if target.exists() else None)!=raw:raise InstallerError('提交前检测到配置并发变化')
                temporary.replace(target)
            if target.read_bytes()!=updated:raise InstallerError('配置读回失败')
            entry['state']='verified';done.append({k:v for k,v in entry.items() if k!='state'});save_journal()
        save_journal(True)
    except OSError as error:raise InstallerError('安装写入中断；恢复记录和备份已保留，可重新核查后继续') from error
    return report
