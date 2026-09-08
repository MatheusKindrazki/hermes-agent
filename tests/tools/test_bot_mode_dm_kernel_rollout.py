"""Enforced DM must promote an inline turn through the execution admitter."""
import json
from unittest.mock import Mock
from agent import durable_admission as da
from tools import bot_mode_dm as dm


def test_inline_turn_is_admitted_at_delivery_boundary(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_KERNEL_V1_MODE','enforce')
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    parent=da.AdmissionOutcome(state='no_work_required',reason_code='inline_answer',model_may_run=True,session_id='session-1',event_id='evt.input.1')
    execution=da.AdmissionOutcome(state='admitted',reason_code='execution_seam',model_may_run=True,session_id='session-1',event_id='evt.effect.1')
    da.bind_admitted_turn(parent)
    admit=Mock(return_value=execution)
    monkeypatch.setattr(da,'admit_execution',admit)
    monkeypatch.setattr(dm,'_delivery_ledger_path',lambda key:tmp_path/('ledger-'+key+'.json'))
    monkeypatch.setattr(dm,'_agent_home',lambda agent:str(tmp_path))
    def revalidate():
        assert da.current_admitted_turn() is execution
        return {'origin_session_id':'session-1','request_key':'request-1'},{}
    monkeypatch.setattr(da,'revalidate_current_turn_identity',revalidate)
    monkeypatch.setattr(dm,'_write_dm_file',lambda content:str(tmp_path/'message'))
    monkeypatch.setattr(dm,'_write_delivery_receipt',lambda *args,**kwargs:{'state':'accepted'})
    monkeypatch.setattr(dm,'_delivery_command',lambda *args,**kwargs:'unused')
    monkeypatch.setattr(dm,'_spawn_delivery',lambda *args,**kwargs:json.dumps({'status':'accepted'}))
    agent=Mock(session_id='session-1')
    try:
        result=json.loads(dm._start_delivery(['/usr/bin/false'],'test body','projetospessoais',stdin_file=False,task_id=None,agent=agent))
        assert result.get('status')=='accepted',result
        assert admit.call_count==1
        assert da.current_admitted_turn() is parent
    finally:da.reset_state_for_tests()


def test_enforced_replay_never_readmits_or_respawns(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_KERNEL_V1_MODE','enforce')
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    da.bind_admitted_turn(da.AdmissionOutcome(state='no_work_required',reason_code='inline_answer',model_may_run=True,session_id='session-1',event_id='evt.input.1'))
    monkeypatch.setattr(dm,'_agent_home',lambda agent:str(tmp_path))
    monkeypatch.setattr(dm,'_delivery_ledger_path',lambda key:tmp_path/('ledger-'+key+'.json'))
    monkeypatch.setattr(dm,'_read_delivery_ledger',lambda path,key:{'state':'delivered','idempotency_key':key})
    admit=Mock(side_effect=AssertionError('re-admission'));spawn=Mock(side_effect=AssertionError('duplicate spawn'))
    monkeypatch.setattr(da,'admit_execution',admit);monkeypatch.setattr(dm,'_spawn_delivery',spawn)
    try:
        result=json.loads(dm._start_delivery(['/usr/bin/false'],'test body','projetospessoais',stdin_file=False,task_id=None,agent=Mock(session_id='session-1')))
        assert result['status']=='delivered'
        admit.assert_not_called();spawn.assert_not_called()
    finally:da.reset_state_for_tests()


def test_enforced_effect_requires_independent_ack_before_release(tmp_path,monkeypatch):
    record={'enforced_effect':True,'idempotency_key':'a'*64,'turn_identity':{},'release_event_id':'event'}
    release=Mock(side_effect=AssertionError('unverified release'))
    monkeypatch.setattr(da,'release_observed_effect',release)
    monkeypatch.setattr(dm,'_delivery_ledger_path',lambda key:tmp_path/'ledger.json')
    dm._settle_enforced_delivery(record,tmp_path/'receipt.json',{},ack_row_validated=False)
    assert record['release_state']=='ack_not_validated'
    release.assert_not_called()


def test_enforced_effect_records_real_release_outcome(tmp_path,monkeypatch):
    record={'enforced_effect':True,'idempotency_key':'a'*64,'turn_identity':{},'release_event_id':'event'}
    release=Mock(return_value={'outcome':'released'})
    monkeypatch.setattr(da,'release_observed_effect',release)
    monkeypatch.setattr(dm,'_emit_delivery_shadow_receipt',lambda *args,**kwargs:None)
    monkeypatch.setattr(dm,'_delivery_ledger_path',lambda key:tmp_path/'ledger.json')
    dm._settle_enforced_delivery(record,tmp_path/'receipt.json',{},ack_row_validated=True)
    assert record['release_state']=='acknowledged'
    assert json.loads((tmp_path/'ledger.json').read_text())['release_receipt']=={'outcome':'released'}
    assert release.call_count==1


def test_gateway_route_mapping_preserves_input_and_exact_chat(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_KERNEL_V1_MODE','enforce')
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setattr(da,'native_input_enabled',lambda:True)
    monkeypatch.setattr(da,'_active_profile',lambda:'financas')
    monkeypatch.setattr(dm,'_agent_home',lambda agent:str(tmp_path))
    monkeypatch.setattr(dm,'_delivery_ledger_path',lambda key:tmp_path/('ledger-'+key+'.json'))
    monkeypatch.setattr(dm,'_read_delivery_ledger',lambda path,key:{'state':'delivered','idempotency_key':key})
    monkeypatch.setattr(da,'admit_execution',Mock(side_effect=AssertionError('re-admission')))
    parent=da.AdmissionOutcome(state='no_work_required',reason_code='inline_answer',model_may_run=True,
                               session_id='agent:main:telegram:42',event_id='telegram.123')
    da.bind_admitted_turn(parent)
    try:
        with da.effect_origin_scope(da.capture_effect_origin(parent.session_id,parent.event_id)):
            origin=da.canonical_effect_origin('real-db-session-42')
            with da.effect_origin_scope(origin):
                def attempt(sid):
                    return json.loads(dm._start_delivery(['/usr/bin/false'],'body','projetospessoais',stdin_file=False,task_id=None,agent=Mock(session_id=sid)))
                assert attempt('real-db-session-42')['status']=='delivered'
                assert 'error' in attempt('another-db-session')
                da.bind_admitted_turn(da.AdmissionOutcome(state='no_work_required',reason_code='inline_answer',model_may_run=True,
                                     session_id=parent.session_id,event_id='telegram.999'))
                assert 'error' in attempt('real-db-session-42')
        assert parent.session_id=='agent:main:telegram:42'
    finally:da.reset_state_for_tests()
