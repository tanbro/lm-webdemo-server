import asyncio
import os
import shlex
from datetime import datetime

from sanic import Sanic, response
from sanic.exceptions import abort
from sanic.log import logger
from sanic.views import HTTPMethodView

from ..app import app

PERSONALITY_TEXT = 'Selected personality:'

proc: asyncio.subprocess.Process = None
lock = asyncio.Lock()
proc_info = {}


class Index(HTTPMethodView):

    def options(self, request):
        return response.raw(b'')

    def get(self, request, id_=None):
        if id_ is None:
            if proc:
                return response.json([proc.pid])
            return response.json([])
        #
        if not proc:
            return response.text('', 404)
        if id_ != proc.pid:
            return response.text('', 404)
        return response.json({
            'id': id_,
            'personality': proc_info['personality'],
            'history': proc_info['history'],
        })

    async def post(self, request):
        global proc, proc_info

        program = app.config.interact_cmd
        args = shlex.split(app.config.interact_args)
        cwd = app.config.interact_pwd

        if lock.locked():
            return response.text('', 409)
        async with lock:
            # 首先关闭
            await terminate_proc()

            # 然后新建
            logger.info(
                'create subprocess: program=%s, args=%s, cwd=%s',
                program, args, cwd
            )
            proc = await asyncio.create_subprocess_exec(
                program,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd
            )
            proc_info = {
                'personality': '',
                'history': [],
            }
            logger.info(
                'subprocess created: %s',
                proc
            )

            # 等待启动
            logger.info('等待 %s 启动 ..', proc)
            personality = ''
            while not personality:
                # stdout stderr 同时读取
                try:
                    outputs = await readline_from_stdout_or_stderr(proc.stdout, proc.stderr, 1)
                except asyncio.TimeoutError:
                    continue
                for name, line in outputs:
                    logger.info('%s %s: %s', proc, name, line)
                    # 是否满足启动时候的输出字符串判断？
                    pos = line.find(PERSONALITY_TEXT)
                    if pos >= 0:
                        personality = line[pos + len(PERSONALITY_TEXT):]
                        personality = personality.strip().lstrip('▁').lstrip()
                        logger.info(
                            '%s 启动成功. personality: %s',
                            proc, personality
                        )
                        break
            proc_info['personality'] = personality
            return response.json(dict(
                id=proc.pid,
                personality=personality,
            ))


class Input(HTTPMethodView):
    def options(self, request, id_):
        return response.raw(b'')

    async def post(self, request, id_):
        if lock.locked():
            return response.text('', 409)
        async with lock:
            if not proc:
                return response.text('', 404)
            if proc.pid != id_:
                return response.text('', 404)
            # 用户输入
            msg = request.json['msg'].strip()
            logger.info('intput: %s', msg)
            proc_info['history'].append({
                'dir': 'input',
                'msg': msg,
                'time': datetime.now().isoformat(),
            })
            # 传到 interact 进程
            data = (msg.strip() + os.linesep).encode()
            proc.stdin.write(data)
            await proc.stdin.drain()
            # 读取 interact 进程 的 stdout/stderr 输出
            msg = ''
            while not msg:
                try:
                    outputs = await readline_from_stdout_or_stderr(proc.stdout, proc.stderr, 1)
                except asyncio.TimeoutError:
                    continue
                for name, line in outputs:
                    logger.info('%s %s: %s', proc, name, line)
                    if name.upper() == 'STDOUT':
                        # 得到了返回消息
                        line = line.lstrip('>').lstrip().lstrip('▁').lstrip()
                        msg += line
                        break
            proc_info['history'].append({
                'dir': 'output',
                'msg': msg,
                'time': datetime.now().isoformat(),
            })
            return response.json(dict(
                msg=msg
            ))


async def terminate_proc():
    global proc
    if proc:
        logger.info('terminate: %s', proc)
        try:
            proc.terminate()
        except ProcessLookupError as e:
            proc = None
            logger.error(
                'ProcessLookupError when terminating %s: %s', proc, e)
        else:
            logger.info('terminate %s: waiting...', proc)
            await proc.wait()
            logger.info('terminate %s: ok. returncode=%s',
                        proc, proc.returncode)
            proc = None
            proc_info = {}


async def readline_from_stdout_or_stderr(stdout_stream, stderr_stream, timeout=None):

    async def readline(name, stream):
        line = await stream.readline()
        return name, line

    result = []

    aws = {
        asyncio.create_task(readline(*args))
        for args in zip(('STDOUT', 'STDERR'), (stdout_stream, stderr_stream))
    }

    done, pending = await asyncio.wait(aws, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()

    for task in done:
        name, data = task.result()
        if data == b'':
            logger.error(
                'EOF on %s. %s was terminated, return code: %s', name, proc, proc.returncode)
            await terminate_proc()
            return response.text('', 500)
        result.append((name, data.decode().strip()))

    return result
