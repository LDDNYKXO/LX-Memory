"""Beginner-facing offline setup; credentials stay in the product's own session."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from .setup import CLIENTS, InstallerError, configure
from .broker_client import call
from .cloud import ConnectorError

def selection(text,defaults):
    if not text.strip():
        if not defaults:raise InstallerError('请至少选择一个助手')
        return defaults
    try:
        indexes=[int(s.strip()) for s in text.split(',')]
        if len(set(indexes))!=len(indexes) or any(not 1<=i<=4 for i in indexes):raise ValueError()
        return [CLIENTS[i-1] for i in indexes]
    except ValueError:raise InstallerError('请输入1至4的编号，用英文逗号分隔') from None

async def diagnose(root):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    parameters=StdioServerParameters(command=sys.executable,args=['-B',str(root/'run.py'),'mcp'],env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8'})
    def unpack(result):return result.structured_content or json.loads(result.content[0].text)
    async with stdio_client(parameters) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            tools=await session.list_tools()
            names=sorted(t.name for t in tools.tools)
            from .maintenance import check_tools
            if not check_tools([{'name':t.name,'input_schema':t.input_schema} for t in tools.tools])['compatible']:
                raise InstallerError('MCP工具缺失或参数不兼容')
            status=unpack(await session.call_tool('lingxi_memory_status',{}))
            report={'mcp_connected':True,'tools':names,'login_state':status.get('state'),'cloud_verified':False,'desktop_clients_native_verified':False}
            if status.get('state')=='CONNECTED':
                result=unpack(await session.call_tool('lingxi_memory_search',{'query':'记忆','limit':1}))
                report['cloud_verified']=result.get('source')=='lingxi_cloud' and 'error' not in result
            return report

def edge_installed():
    candidates=[Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe',Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'Microsoft/Edge/Application/msedge.exe',Path.home()/'AppData/Local/Microsoft/Edge/Application/msedge.exe']
    return any(p.exists() for p in candidates)

def main(root):
    parser=argparse.ArgumentParser(description='灵犀记忆安装入口：选择助手、正常登录、检查连接')
    parser.add_argument('--clients',help='Codex,DSH,WorkBuddy,ZCode；无参数时交互选择')
    parser.add_argument('--apply',action='store_true',help='非交互模式实际配置；省略则只预览')
    parser.add_argument('--skip-login',action='store_true',help='不打开登录或保存功能选择，只核查现有连接')
    options=parser.parse_args()
    if (root/'MANIFEST-SHA256.json').exists():
        from .release import verify_manifest
        try:verify_manifest(root)
        except (ValueError,OSError):raise InstallerError('安装包不完整或校验失败，请重新解压完整ZIP') from None
    interactive=options.clients is None
    home=Path.home();python=Path(sys.executable).resolve()
    if not edge_installed():raise InstallerError('未找到Microsoft Edge。请先安装Edge，再运行本入口。')
    if interactive:
        print('灵犀记忆安装\n请选择这台电脑要接入的助手：')
        directories={ 'Codex':'.codex','DSH':'.dsh','WorkBuddy':'.workbuddy','ZCode':'.zcode'}
        defaults=[c for c in CLIENTS if (home/directories[c]).exists()]
        for index,client in enumerate(CLIENTS,1):print(f'{index}. {client}'+('（检测到配置目录）' if client in defaults else ''))
        chosen=selection(input('输入编号，如1,3；直接回车选择检测到的助手：'),defaults)
    else:chosen=[c.strip() for c in options.clients.split(',')]
    preview=configure(root,home,python,chosen,apply=False)
    if not interactive and not options.apply:
        print(json.dumps(preview,ensure_ascii=False,indent=2));return
    result=configure(root,home,python,chosen,apply=True)
    (root/'runtime/python.txt').write_text(str(python),encoding='utf-8')
    print('助手配置和技能已准备：'+', '.join(chosen))
    try:
        status=call(root,'status')
        if interactive and not options.skip_login and status.get('state')!='CONNECTED':
            remember=input('是否保存本机登录，方便下次使用？[y/N]：').strip().lower() in ('y','yes','是')
            call(root,'login',{'remember':remember})
            input('请在产品打开的Edge窗口完成正常登录；完成后回到这里按回车。')
        check=asyncio.run(diagnose(root))
        result['connection_check']=check
        if check['cloud_verified']:
            write=call(root,'write-status')
            if interactive and not options.skip_login and not write['write_enabled']:
                print('开启保存功能时会新增1条校验记忆，并验证重复创建被拒绝。以后仅在你明确要求保存时写入。')
                if input('是否开启保存功能？[y/N]：').strip().lower() in ('y','yes','是'):
                    probe=call(root,'enable-saving')
                    result['saving_check']=probe
                    write=call(root,'write-status')
            result['saving_status']=write
            print('已连接灵犀云端；保存功能：'+('已开启' if write['write_enabled'] else '尚未开启'))
        else:print('MCP已接通，尚未验证云端登录。再次打开安装入口即可继续登录。')
    except ConnectorError as e:
        result['connection_error']=e.code
        print('配置已保留，连接检查返回：'+e.code+'。可再次打开本入口继续。')
    (root/'runtime/session/last-setup-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('如助手仍显示旧工具，请刷新其MCP连接或在当前任务结束后重启一次。')
    print('以后可直接说“查询灵犀记忆”或“请记住：……”；无需复制配置和凭据。')

if __name__=='__main__':raise RuntimeError('Use setup_wizard.py')
