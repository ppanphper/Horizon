"""Run the shipped form logic in Node, with only DOM/network boundaries stubbed."""
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_ui(code):
    harness = r'''
const fs = require('fs'), vm = require('vm');
const nodes = {};
const document = {getElementById(id) {return nodes[id] ||= {_value:'', get value(){return this._value;}, set value(v){this._value=String(v);}, checked:false};}};
const context = vm.createContext({document, console, Option: function(){}});
const source = fs.readFileSync('src/web/static/app.js', 'utf8');
vm.runInContext(source.split('document.querySelectorAll("nav button")')[0], context);
vm.runInContext(source.slice(source.indexOf('let profileDetail ='), source.indexOf('$("profile-picker").addEventListener')), context);
vm.runInContext(`renderSources = renderRules = updateOverview = setProfiles = () => {};
state = {ai:{model:'expanded-model',base_url:'https://example.com/v1'},profiles:[{id:'tech-news'}]};
draft = {ai:{provider:'openai',model:'\u0024{MODEL}',base_url:'\u0024{BASE}'}, sources:{}, processing:{profile_settings:{'tech-news':{threshold:4}}}};
fillForm(); $('default-profile').value='tech-news'; $('threshold-tech-news').value='4'; $('dedup-tech-news').checked=true;
`, context);
Promise.resolve(vm.runInContext(process.argv[1], context)).then(result => console.log(JSON.stringify(result)));
'''
    result = subprocess.run(["node", "-e", harness, code], cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("field,expected", [("model", "${MODEL}"), ("base_url", "${BASE}")])
def test_unrelated_save_preserves_environment_reference(field, expected):
    assert run_ui(f'collect().ai.{field}') == expected


def test_unrelated_save_preserves_omitted_topic_dedup():
    assert run_ui('collect().processing.profile_settings["tech-news"]') == {"threshold": 4}


def test_create_from_current_content_rewrites_identity_and_prompt_paths():
    body = run_ui('''(async () => {
      profileDetail = {id:'tech-news',revision:'old'};
      $('profile-definition').value = JSON.stringify({id:'tech-news',name:'Edited',match:'custom-match.md',analysis:'custom-analysis.md',enrichment:{prompt:'custom-enrichment.md',blocks:[]}});
      $('profile-new-id').value='my-news';
      let sent;
      api = async (path, body) => {sent=body;};
      load = loadProfiles = async () => {}; message = () => {};
      await profileAction('create');
      return sent;
    })()''')
    assert body["id"] == "my-news"
    assert body["definition"]["id"] == "my-news"
    assert body["definition"]["name"] == "Edited"
    assert body["definition"]["match"] == "match.md"
    assert body["definition"]["analysis"] == "analysis.md"
    assert body["definition"]["enrichment"]["prompt"] == "enrichment.md"


def test_explicit_model_edit_is_saved():
    assert run_ui('$("model").value="edited"; collect().ai.model') == "edited"
