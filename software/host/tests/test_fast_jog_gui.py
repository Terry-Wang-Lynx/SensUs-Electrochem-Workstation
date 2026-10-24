from test_automation_gui import evaluate
from test_keyboard_alignment_gui import evaluate_keyboard_page


def test_hold_copy_distinguishes_pending_commands_from_total_travel():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "pa_host" / "gui" / "high_throughput.js").read_text()
    assert "持续按住最多 30 s / 10 mm" not in source
    assert "不是总行程限制" in source
    assert "按住持续移动，松开停止" in source


def test_20mm_settings_require_clearance_but_low_speed_remains_backwards_compatible():
    result = evaluate("""
let rejected=false;try{ui.validateJogSettings(20,20);}catch(error){rejected=true;}
console.log(JSON.stringify({rejected,fast:ui.validateJogSettings(20,20,true),fine:ui.validateJogSettings(.5,1)}));
""")
    assert result == {"rejected": True, "fast": {"speed_mm_s": 20, "max_speed_mm_s": 20,
                      "fast_motion_confirmed": True}, "fine": {"speed_mm_s": 0.5, "max_speed_mm_s": 1}}


def test_fast_preset_does_not_apply_until_explicit_clearance_confirmation():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual.active=true;sessionGuided.manual.status='aligning';
 sessionGuided.manual.alignment.settings.hard_max_speed_mm_s=20;
 HighThroughput.activate();await tick();await click('htCalibrationToggle');
 await click('htJogFast');await click('htJogApply');
 const before=requests.filter(request=>request.route.endsWith('/jog/config')).length;
 elements.get('htJogFastConfirmed').checked=true;change('htJogFastConfirmed');await click('htJogApply');
 const configs=requests.filter(request=>request.route.endsWith('/jog/config'));
 console.log(JSON.stringify({before,configs:configs.map(item=>item.body)}));
})();
""")
    assert result["before"] == 0
    assert result["configs"] == [{"session_token": "keyboard-session", "speed_mm_s": 20,
                                  "max_speed_mm_s": 20, "fast_motion_confirmed": True}]
