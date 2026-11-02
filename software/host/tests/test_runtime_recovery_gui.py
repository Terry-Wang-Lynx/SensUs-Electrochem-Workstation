import pytest

from test_automation_gui import evaluate_browser
from test_keyboard_alignment_gui import KEYBOARD_BOOTSTRAP


def test_verified_arrival_timeout_guidance_offers_reference_recovery_first():
    from test_automation_gui import evaluate
    result = evaluate("""
console.log(JSON.stringify(ui.errorGuidance({code:'execution_failed',cause:{code:'move_timeout'}},
 {active:false},{stop_confirmed:true,recovery_required:false})));
""")
    assert '原位核验并恢复标定' in result
    assert '核验不通过' in result


@pytest.mark.parametrize('faulted,stop_confirmed,recovery_required,expected', [
    (False, True, False, True), (True, True, False, False),
    (False, False, False, False), (False, True, True, False),
])
def test_old_experiment_error_does_not_disable_healthy_alignment(faulted, stop_confirmed, recovery_required, expected):
    bootstrap = KEYBOARD_BOOTSTRAP + """
controllerSnapshot.state='error';
controllerSnapshot.motion.faulted=FAULT;
controllerSnapshot.stop_confirmed=STOP;
controllerSnapshot.recovery_required=RECOVERY;
sessionGuided.manual.active=true;
sessionGuided.manual.status='aligning';
controllerSnapshot.busy=true;
""".replace('FAULT', str(faulted).lower()).replace('STOP', str(stop_confirmed).lower()).replace('RECOVERY', str(recovery_required).lower())
    result = evaluate_browser("""
(async()=>{
 root.classList.toggle('active',true);HighThroughput.activate();await tick();await click('htCalibrationToggle');
 const enabled=!elements.get('htJogLeft').disabled;
 dispatchKey('keydown','a');await tick();dispatchKey('keyup','a');await tick();
 console.log(JSON.stringify({enabled,starts:requests.filter(r=>r.route.endsWith('/jog/start')).length}));
})();
""", before_source=bootstrap)
    assert result['enabled'] is expected
    assert result['starts'] == (1 if expected else 0)
