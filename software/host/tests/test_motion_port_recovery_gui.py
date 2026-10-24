import pytest

from test_automation_gui import evaluate_browser
from test_keyboard_alignment_gui import KEYBOARD_BOOTSTRAP


@pytest.mark.parametrize("confirmed", [True, False])
def test_missing_port_reset_requires_same_bus_confirmation_and_never_moves(confirmed):
    bootstrap = KEYBOARD_BOOTSTRAP + """
sessionGuided.manual={active:false,status:'idle',points:{},alignment:{status:'idle'}};
sessionGuided.document.bindings.motion={port:'old-port'};
controllerSnapshot.motion={connected:false,port:'old-port',faulted:true,axes:{}};
controllerSnapshot.stop_confirmed=false;controllerSnapshot.recovery_required=true;
const confirmations=[];
globalThis.confirm=message=>{confirmations.push(message);return CONFIRMED;};
const portRecoveryFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 if(route==='ports')return limitsResponse({ports:[{device:'replacement-port',description:'USB Serial',hwid:'USB VID:PID=1A86:7523'}]});
 if(route==='motion/reset'){
  const body=JSON.parse(options.body);requests.push({route,body});
  return limitsResponse({state:controllerSnapshot,guided:sessionGuided,motion:controllerSnapshot.motion});
 }
 return portRecoveryFetch(path,options);
};
""".replace("CONFIRMED", str(confirmed).lower())
    result = evaluate_browser("""
(async()=>{
 root.classList.toggle('active',true);HighThroughput.activate();
 for(let step=0;step<5;step++)await tick();
 await click('htResetDevices');for(let step=0;step<8;step++)await tick();
 console.log(JSON.stringify({confirmations,reset:requests.filter(item=>item.route==='motion/reset'),
  starts:requests.filter(item=>item.route==='guided/calibration/begin'||item.route.endsWith('/jog/start'))}));
})();
""", before_source=bootstrap)
    assert len(result["confirmations"]) == 1
    assert "old-port" in result["confirmations"][0]
    assert "replacement-port" in result["confirmations"][0]
    assert result["starts"] == []
    if confirmed:
        assert result["reset"][0]["body"] == {"port": "replacement-port", "same_bus_confirmed": True}
    else:
        assert result["reset"] == []
