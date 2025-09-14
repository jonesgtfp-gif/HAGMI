import hashlib
import logging
from typing import Any, AsyncGenerator
import time
import os
import typing
import json
import asyncio
import traceback
import random
import string
import aiohttp
import contextlib
import pathlib
from models.aistudio import PromptHistory, flatten, inflate, StreamParser, Model, ListModelsResponse
from datetime import datetime, timezone
from camoufox.async_api import AsyncCamoufox, AsyncNewBrowser
from playwright.async_api import Route, expect, async_playwright, BrowserContext, Browser, Page
import dataclasses
from config import config
from utils import Profiler, CredentialManager, Credential


logger = logging.getLogger('Browser')


def sapisidhash(cookies: dict[str, str]) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    sapisid: str = cookies.get(
        '__Secure-3PAPISID',
        cookies.get('SAPISID', cookies.get('__Secure-1PAPISID'))) or ''
    assert sapisid
    m = hashlib.sha1(' '.join(
        (str(now), sapisid, "https://aistudio.google.com")).encode())
    sapisidhash = '_'.join((str(now), m.hexdigest()))
    return ' '.join(
        f'{key} {sapisidhash}' for key in ('SAPISIDHASH', 'SAPISID1PHASH', 'SAPISID3PHASH'))



fixed_responsed = [
    [
        [
            [[[[[None,"**Thinking**\n\n不，你不想。\n\n\n",None,None,None,None,None,None,None,None,None,None,1]],"model"]]],
            None,[6,None,74,None,[[1,6]],None,None,None,None,68]],
        [
            [[[[[None,"摆。"]],"model"],1]],
            None,[6,9,215,None,[[1,6]],None,None,None,None,200]],
        [
            None,None,None,["1749019849541811",109021497,4162363067]]
    ]
]


@dataclasses.dataclass
class InterceptTask:
    prompt_history: PromptHistory
    future: asyncio.Future
    profiler: Profiler


class BrowserPool:
    queue: asyncio.Queue[InterceptTask]

    def __init__(self, credentialManager: CredentialManager, worker_count: int = config.WorkerCount, *, endpoint: str | None = None, loop=None):
        self._loop = loop
        self._endpoint = endpoint
        self.queue = asyncio.Queue()
        self._credMgr = credentialManager
        self._worker_count = min(worker_count, len(self._credMgr.credentials))
        self._workers: list[asyncio.Task] = []
        self._models: list[Model] = []

    def set_Models(self, models: list[Model]):
        self._models = models

    def get_Models(self) -> list[Model]:
        #TODO: model list override
        return self._models

    async def start(self):

        for cred in self._credMgr.credentials[:self._worker_count]:

            worker = BrowserWorker(
                credential=cred,
                pool=self,
                endpoint=self._endpoint,
                loop=self._loop
            )
            logger.info('spawn worker with %s', cred.email)
            task = asyncio.create_task(worker.run(), name=f"BrowserWorker-{cred.email}")
            self._workers.append(task)
        return self

    async def stop(self):
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def put_task(self, task: InterceptTask):
        await self.queue.put(task)


class BrowserWorker:
    _browser: Browser | None
    _studio_browser: BrowserContext | None
    _login_browser: BrowserContext | None
    _credential: Credential
    _endpoint: str | None
    _pool: BrowserPool
    status: str

    def __init__(self, credential: Credential, pool: BrowserPool, *, endpoint: str|None = None, loop=None):
        self._loop = loop
        self._credential = credential
        self._browser = None
        self._studio_browser = None
        self._login_browser = None
        self._endpoint = endpoint
        self._pages = []
        self._pool = pool

    async def browser(self) -> BrowserContext:
        if not self._studio_browser:
            if not self._browser:
                self._browser = typing.cast(Browser, await AsyncCamoufox(
                    headless=config.Headless,
                    main_world_eval=True,
                    enable_cache=True,
                    locale="US",
                ).__aenter__())

            storage_state = None
            state_path = f'{config.StatesDir}/{self._credential.stateFile}'
            if self._credential.stateFile and os.path.exists(state_path):
                storage_state = state_path
            else:
                # This should not happen if validate_state is called first
                raise Exception(f"State file not found for {self._credential.email}")

            self._studio_browser = await self._browser.new_context(
                storage_state=storage_state,
                ignore_https_errors=True,
                locale="US",
            )
            self._pages = list(self._studio_browser.pages)
        return self._studio_browser

    async def handle_ListModels(self, route: Route) -> None:
        if not self._pool.get_Models():
            resp = await route.fetch()
            data = inflate(await resp.json(), ListModelsResponse)
            if data:
                self._pool.set_Models(data.models)
        # TODO: 从缓存快速返回请求
        # TODO: 劫持并修改模型列表
        await route.fallback()

    async def prepare_page(self, page: Page) -> None:
        await page.route("**/www.google-analytics.com/*", lambda route: route.abort())
        await page.route("**/play.google.com/*", lambda route: route.abort())
        await page.route("**/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListModels", self.handle_ListModels)
        if not page.url.startswith(config.AIStudioUrl):
            await page.goto(f'{config.AIStudioUrl}/prompts/new_chat')
            await page.evaluate("""()=>{mw:localStorage.setItem("aiStudioUserPreference", '{"isAdvancedOpen":false,"isSafetySettingsOpen":false,"areToolsOpen":true,"autosaveEnabled":false,"hasShownDrivePermissionDialog":true,"hasShownAutosaveOnDialog":true,"enterKeyBehavior":0,"theme":"system","bidiOutputFormat":3,"isSystemInstructionsOpen":true,"warmWelcomeDisplayed":true,"getCodeLanguage":"Python","getCodeHistoryToggle":true,"fileCopyrightAcknowledged":false,"enableSearchAsATool":true,"selectedSystemInstructionsConfigName":null,"thinkingBudgetsByModel":{},"rawModeEnabled":false,"monacoEditorTextWrap":false,"monacoEditorFontLigatures":true,"monacoEditorMinimap":false,"monacoEditorFolding":false,"monacoEditorLineNumbers":true,"monacoEditorStickyScrollEnabled":true,"monacoEditorGuidesIndentation":true}')}""")

    async def validate_state(self) -> bool:
        state_path = f'{config.StatesDir}/{self._credential.stateFile}'
        # Check if state file is valid by trying to go to AI Studio
        if os.path.exists(state_path):
            async with self.page() as page:
                await page.goto(f'{config.AIStudioUrl}/prompts/new_chat')
                if page.url.startswith(config.AIStudioUrl):
                    logger.info('State file is valid for %s', self._credential.email)
                    return True
                logger.info('State file is invalid for %s, re-login required.', self._credential.email)

        # No valid state file, perform login
        if not self._credential.email or not self._credential.password:
            raise Exception(f"No valid state file and no credentials to login for {self._credential.email}")

        logger.info('Performing login for %s', self._credential.email)
        p = await async_playwright().__aenter__()
        browser = await p.firefox.launch(headless=config.Headless)
        context = await browser.new_context(locale="US", ignore_https_errors=True)
        page = await context.new_page()

        try:
            await page.goto('https://accounts.google.com/')
            
            logger.info('login using credential %s', self._credential.email)
            await page.locator('input#identifierId').type(self._credential.email)
            await expect(page.locator('#identifierNext button')).to_be_enabled()
            await page.locator('#identifierNext button').click()
            await asyncio.sleep(3)
            await expect(page.locator('input[name="Passwd"]')).to_be_editable()
            logger.info('login using credential %s type in password', self._credential.email)
            await page.locator('input[name="Passwd"]').type(self._credential.password)
            await expect(page.locator('#passwordNext button')).to_be_enabled()
            await page.locator('#passwordNext button').click()
            
            # Wait for successful login, e.g., by checking for a URL that indicates success
            await page.wait_for_url("**/myaccount.google.com/**", timeout=60000)
            
            await context.storage_state(path=state_path)
            logger.info('store state for credential %s', self._credential.email)
            return True
        finally:
            await browser.close()
            await p.__aexit__()

    @contextlib.asynccontextmanager
    async def page(self):
        if not self._pages:
            page = await (await self.browser()).new_page() # This now correctly uses the camouflaged browser
        else:
            page = self._pages.pop(0)
        await self.prepare_page(page)
        try:
            yield page
        finally:
            self._pages.append(page)
            await page.unroute_all()

    async def run(self):
        await self.validate_state()
        logger.info('Worker %s is ready', self._credential.email)
        while True:
            try:
                task = await self._pool.queue.get()
                task.profiler.span('worker: task fetched')
                await self.InterceptRequest(task.prompt_history, task.future, task.profiler)
            except Exception as exc:
                task.profiler.span('worker: failed with exception', traceback.format_exception(exc))
                task.future.set_exception(exc)

    async def InterceptRequest(self, prompt_history: PromptHistory, future: asyncio.Future, profiler: Profiler, timeout: int=60):
        prompt_id = prompt_history.prompt.uri.split('/')[-1]

        async def handle_route(route: Route) -> None:
            match route.request.url.split('/')[-1]:
                case 'GenerateContent':
                    profiler.span('Route: Intercept GenerateContent')
                    future.set_result((route.request.headers, route.request.post_data))
                    await route.fulfill(
                        content_type='application/json+protobuf; charset=UTF-8',
                        body=json.dumps(fixed_responsed, separators=(',', ':'))
                    )
                case 'ResolveDriveResource':
                    profiler.span('Route: serve PromptHistory')
                    data = json.dumps(flatten(prompt_history), separators=(',', ':'))
                    await route.fulfill(
                        content_type='application/json+protobuf; charset=UTF-8',
                        body=data,
                    )
                case 'CreatePrompt':
                    await route.abort()
                case 'UpdatePrompt':
                    await route.abort()
                case 'ListPrompts':
                    profiler.span('Route: serve ListPrompts')
                    data = json.dumps(flatten([prompt_history.prompt.promptMetadata]), separators=(',', ':'))
                    await route.fulfill(
                        content_type='application/json+protobuf; charset=UTF-8',
                        body=data,
                    )
                case 'CountTokens':
                    await route.fulfill(
                        content_type='application/json+protobuf; charset=UTF-8',
                        body=json.dumps([4,[],[[[3],1]],None,None,[[1,4]]],  separators=(',', ':'))
                    )
                case _:
                    await route.fallback()

        async with asyncio.timeout(timeout):
            async with self.page() as page:
                profiler.span('Page: Created')
                await page.route("**/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/*", handle_route)
                await page.goto(f'{config.AIStudioUrl}/prompts/{prompt_id}')
                profiler.span('Page: Loaded')
                last_turn = page.locator('ms-chat-turn').last
                await expect(last_turn.locator('ms-text-chunk')).to_have_text('(placeholder)', timeout=20000)
                profiler.span('Page: Placeholder Visible')
                if await page.locator('.glue-cookie-notification-bar__reject').is_visible():
                    await page.locator('.glue-cookie-notification-bar__reject').click()
                if await page.locator('button[aria-label="Close run settings panel"]').is_visible():
                    await page.locator('button[aria-label="Close run settings panel"]').click(force=True)
                await page.locator('ms-text-chunk textarea').click()
                await last_turn.click(force=True)
                profiler.span('Page: Last Turn Hover')
                rerun = last_turn.locator('[name="rerun-button"]')
                await expect(rerun).to_be_visible()
                profiler.span('Page: Rerun Visible')
                await rerun.click(force=True)
                profiler.span('Page: Rerun Clicked')
                await page.locator('ms-text-chunk textarea').click()
                await future
                await page.unroute("**/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/*")
