import json
import shutil
import subprocess

import pytest

from telegram_media_bot.infrastructure.web_companion.pages import CONNECT_HTML, CONNECT_JS


def test_browser_login_uses_one_exchange_and_two_factor_without_retained_password() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required only for the browser-script simulator")
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = new Map(); let submitHandler, resolveExchange; const calls = [];
for (const id of ['connect-form','submit','status','username','password','code','code-label','consent']) {
  elements.set(id, {value:'',disabled:true,hidden:false,checked:true,
    addEventListener: (_, handler) => {submitHandler=handler;}});
}
const context = {URLSearchParams,location:{hash:'#handoff=one-time-fixture',pathname:'/instagram/connect',protocol:'https:'},
  document:{getElementById:id=>elements.get(id)},history:{replaceState:()=>{context.location.hash='';}},
  fetch:(url,options)=>{calls.push({url,body:JSON.parse(options.body)});
    if(url.endsWith('/exchange')) return new Promise(resolve=>{resolveExchange=resolve;});
    const completes=calls.filter(call=>call.url.endsWith('/complete')).length;
    return Promise.resolve({ok:true,status:200,json:async()=>({stage:completes===1?'need_2fa':'connected'})});
  }};
vm.runInNewContext(SCRIPT, context);
(async()=>{
  elements.get('username').value='fixture-user'; elements.get('password').value='fixture-password'; // pragma: allowlist secret - browser simulator only
  const event={preventDefault:()=>{}};
  const first=submitHandler(event), duplicate=submitHandler(event);
  resolveExchange({ok:true,json:async()=>({csrf_token:'csrf-fixture'})});
  await Promise.all([first,duplicate]);
  assert.equal(calls.length,2); assert.equal(calls[0].url,'/instagram/connect/exchange');
  assert.equal(context.location.hash,''); assert.equal(elements.get('password').value,'');
  assert.equal(calls[1].body.input.consent,'yes'); assert.equal(elements.get('code').disabled,false);
  elements.get('code').value='123456'; await submitHandler(event);
  assert.deepEqual(calls[2].body.input,{code:'123456'});
  assert.equal(elements.get('code').value,''); assert.equal(elements.get('connect-form').hidden,true);
})().catch(error=>{process.stderr.write(error.message);process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-"],
        input="const SCRIPT=" + json.dumps(CONNECT_JS) + ";\n" + harness,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "<style>" not in CONNECT_HTML
    assert "localStorage" not in CONNECT_JS
