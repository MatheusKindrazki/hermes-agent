import uuid
from types import SimpleNamespace
import pytest
from agent import durable_admission as da

@pytest.mark.parametrize('profile',['default','projetospessoais','applausestaff','luguistaff','moklabsstaff'])
def test_enforce_keeps_native_ids_distinct_and_refuses_content_rebind(tmp_path,monkeypatch,profile):
    monkeypatch.setenv('HERMES_KERNEL_V1_MODE','enforce')
    monkeypatch.setattr(da,'_active_profile',lambda:profile)
    monkeypatch.setattr(da,'_settings',lambda:{'mode':'enforce','tenant':'personal','machine':'mini','profile':profile,'state_dir':str(tmp_path)})
    first,second=str(uuid.uuid4()),str(uuid.uuid4())
    assert da.bind_native_prompt_source('session-1',first,'same text')
    assert da.bind_native_prompt_source('session-1',second,'same text')
    assert not da.bind_native_prompt_source('session-1',first,'changed text')
    origins=[da.capture_effect_origin('session-1','native.'+event) for event in (first,second)]
    captured=[]
    def admit(**kwargs):
        captured.append(kwargs['event_id'])
        return da.AdmissionOutcome(state='no_work_required',reason_code='inline_answer',model_may_run=True)
    monkeypatch.setattr(da,'admit_input',admit)
    for origin in origins:
        with da.effect_origin_scope(origin):
            assert da.admit_turn_or_block(SimpleNamespace(session_id='session-1'),'same text') is None
    assert captured==[o.event_id for o in origins]
    assert captured[0]!=captured[1]
    da.reset_state_for_tests()
