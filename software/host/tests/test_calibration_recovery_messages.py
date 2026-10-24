import pytest

from test_automation_gui import evaluate, evaluate_browser
from test_keyboard_alignment_gui import KEYBOARD_BOOTSTRAP, evaluate_keyboard_page


def test_failed_begin_shows_actionable_chinese_separate_from_technical_details():
    result = evaluate_browser("""
(async()=>{
 root.classList.toggle('active',true);HighThroughput.activate();await tick();
 await click('htCalibrationToggle');
 elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');
 await click('htCalibrationBegin');
 console.log(JSON.stringify({message:elements.get('htError').textContent,
  details:elements.get('htErrorDetailText')?.textContent,expanded:!!elements.get('htErrorDetails')?.open}));
})();
""", before_source=KEYBOARD_BOOTSTRAP + """
const originalFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>path.endsWith('/calibration/begin')
 ?{ok:false,status:409,json:async()=>({error:{code:'manual_calibration_active',message:'Finish or safely cancel manual calibration first',request_id:'11947-test'}})}
 :originalFetch(path,options);
""")
    assert "若要" in result["message"]
    assert "请先" in result["message"]
    assert "取消标定" in result["message"]
    assert "manual_calibration_active" not in result["message"]
    assert "Finish or safely cancel" not in result["message"]
    assert "11947-test" not in result["message"]
    assert "manual_calibration_active" in result["details"]
    assert "11947-test" in result["details"]
    assert result["expanded"] is False


@pytest.mark.parametrize("stop_confirmed", [True, False])
def test_blocked_calibration_guidance_does_not_send_user_back_to_broken_cancel(stop_confirmed):
    result = evaluate("""
const manual={active:true,status:'blocked',control_mode:'keyboard',alignment:{stop_result:{stop_confirmed:""" + str(stop_confirmed).lower() + """}}};
console.log(JSON.stringify(ui.errorGuidance({code:'manual_calibration_active',message:'Finish or safely cancel manual calibration first'},manual)));
""")
    assert "重新开始键盘标定" in result
    assert "取消标定" not in result
    assert "请先" in result
    if not stop_confirmed:
        assert "停止" in result
        assert "尚未确认" in result


def test_blocked_snapshot_replaces_english_error_with_persistent_recovery_steps():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);
 sessionGuided.manual={...sessionGuided.manual,active:true,status:'blocked',
  error:{code:'response_length',message:'Unexpected serial response length'}};
 sessionGuided.manual.alignment={...sessionGuided.manual.alignment,status:'blocked',
  stop_result:{stop_confirmed:true},error:sessionGuided.manual.error};
 HighThroughput.activate();await tick();
 console.log(JSON.stringify({progress:elements.get('htCalibrationProgress').textContent,
  jog:elements.get('htJogStatus').textContent}));
})();
""")
    for text in result.values():
        assert "Unexpected serial response length" not in text
    assert "重新开始键盘标定" in result["progress"]
    assert "取消标定" not in result["progress"]


def test_unknown_english_error_is_kept_in_details_not_user_instructions():
    result = evaluate("""
console.log(JSON.stringify(ui.errorGuidance({code:'new_driver_fault',message:'Internal state machine failure'})));
""")
    assert "Internal state machine failure" not in result
    assert "技术详情" in result


def test_unknown_stop_message_also_explains_original_motor_format_rejection():
    result = evaluate("""
const error={code:'alignment_stop_unknown',cause:{code:'response_length',wire:{pending_hex:'0100ee6b'}}};
console.log(JSON.stringify(ui.errorGuidance(error)));
""")
    assert "地址 1" in result
    assert "命令格式错误" in result
    assert "尚未确认双轴停止" in result


def test_old_session_stop_cannot_hide_current_controller_recovery_requirement():
    result = evaluate("""
const manual={active:true,status:'blocked',alignment:{stop_result:{stop_confirmed:true}}};
console.log(JSON.stringify(ui.errorGuidance({code:'manual_calibration_active'},manual,{recovery_required:true})));
""")
    assert "双轴停止尚未确认" in result
    assert "点击「停止」" in result


def test_interrupted_session_can_request_a_new_stop_check_without_an_old_manager():
    result = evaluate("""
const manual={active:true,status:'blocked',error:{code:'manual_interrupted'},alignment:{stop_result:null}};
console.log(JSON.stringify(ui.errorGuidance(manual.error,manual,{stop_confirmed:true,recovery_required:false})));
""")
    assert "先复核双轴停止" in result
    assert "勾选本次运动确认" in result
    assert "再点击「停止」" not in result


def test_polled_background_fault_has_its_own_current_technical_details_and_clears():
    result = evaluate_keyboard_page("""
(async()=>{
 sessionGuided.manual={...sessionGuided.manual,active:true,status:'blocked',
  error:{code:'transaction_timeout',message:'Axis 2 opcode 0x36 response timeout'}};
 HighThroughput.activate();await tick();
 const details=elements.get('htCalibrationErrorDetails'),text=elements.get('htCalibrationErrorDetailText');
 const first={visible:details?.hidden===false,text:text?.textContent,expanded:!!details?.open};
 sessionGuided.manual.error={code:'new_background_fault',message:'Changed background diagnostic'};
 HighThroughput.activate();await tick();
 const latest=text?.textContent;
 sessionGuided.manual={...sessionGuided.manual,status:'aligning',error:null};
 HighThroughput.activate();await tick();
 console.log(JSON.stringify({first,latest,cleared:details?.hidden===true,requests:requests.filter(item=>item.body!==null)}));
})();
""")
    assert result["first"]["visible"] is True
    assert "transaction_timeout" in result["first"]["text"]
    assert "Axis 2 opcode 0x36 response timeout" in result["first"]["text"]
    assert result["first"]["expanded"] is False
    assert "Changed background diagnostic" in result["latest"]
    assert "transaction_timeout" not in result["latest"]
    assert result["cleared"] is True
    assert result["requests"] == []
