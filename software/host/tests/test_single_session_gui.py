from test_absolute_limits_gui import LIMITS_BOOTSTRAP
from test_automation_gui import evaluate_browser


SESSION_BOOTSTRAP = LIMITS_BOOTSTRAP + """
const sessionToken='00000000-0000-0000-0000-000000000004';
const sessionAxis=enabled=>({enabled,speed_rpm:0,telemetry_valid:true,feedback_age_s:0.1});
controllerSnapshot={...controllerSnapshot,motion:{connected:true,connection_token:sessionToken,faulted:false,axes:{X:sessionAxis(false),Z:sessionAxis(false)}}};
let sessionGuided={document:experiment,four_point_required:true,calibration:null,manual:{active:false,supported:true,status:'idle',required_points:['first','last','working','safe'],points:{},error:null}};
let finishFailure=false;
const sessionFetch=globalThis.fetch;
const capturedPoint=point=>({physical_column:point==='first'?1:point==='last'?12:null,raw_counts:{X:point==='last'?3604480:32768,Z:point==='working'?360448:32768}});
const completeCalibration=()=>({id:'current-session',usable:true,status:'saved',four_point:true,connection_token:sessionToken,points:Object.fromEntries(['first','last','working','safe'].map(point=>[point,capturedPoint(point)]))});
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 const body=options.body?JSON.parse(options.body):null;
 if(route==='guided')return limitsResponse(sessionGuided);
 if(route==='guided/preview'){requests.push({route,body});return limitsResponse({plan_hash:'four-point-session-plan',steps:[],duration_s:0});}
 if(route==='guided/run'){requests.push({route,body});return limitsResponse(controllerSnapshot);}
 if(!route.startsWith('guided/calibration/'))return sessionFetch(path,options);
 requests.push({route,body});
 if(route.endsWith('/begin'))sessionGuided={...sessionGuided,calibration:null,manual:{...sessionGuided.manual,active:true,status:'released',points:{},error:null}};
 if(route.endsWith('/capture'))sessionGuided.manual.points[body.point]=capturedPoint(body.point);
 if(route.endsWith('/finish')){
  if(finishFailure){sessionGuided.manual.status='blocked';sessionGuided.manual.error={code:'manual_relock_target',message:'Controller target differs from current position'};return {ok:false,status:409,json:async()=>({error:sessionGuided.manual.error})};}
  sessionGuided={...sessionGuided,calibration:completeCalibration(),manual:{...sessionGuided.manual,active:false,status:'complete'}};
  controllerSnapshot.motion.axes={X:sessionAxis(true),Z:sessionAxis(true)};
 }
 return limitsResponse(sessionGuided);
};
const confirmRelease=()=>{for(const id of ['htCalibrationReleaseConfirmed','htCalibrationZSupported']){elements.get(id).checked=true;change(id);}};
"""


def evaluate_session(script):
    return evaluate_browser(script, before_source=SESSION_BOOTSTRAP)


def test_release_requires_both_explicit_confirmations_before_any_calibration_write():
    result = evaluate_session("""
(async()=>{
 HighThroughput.activate();await tick();
 const supported=elements.has('htCalibrationZSupported');
 if(!supported){console.log(JSON.stringify({supported}));return;}
 const initiallyDisabled=elements.get('htCalibrationBegin').disabled;
 await click('htCalibrationBegin');
 const unconfirmedWrites=requests.filter(request=>request.body!==null);
 elements.get('htCalibrationReleaseConfirmed').checked=true;change('htCalibrationReleaseConfirmed');
 const supportRequired=elements.get('htCalibrationBegin').disabled;
 await click('htCalibrationBegin');
 confirmRelease();const confirmedEnabled=!elements.get('htCalibrationBegin').disabled;
 await click('htCalibrationBegin');
 console.log(JSON.stringify({supported,initiallyDisabled,unconfirmedWrites,supportRequired,confirmedEnabled,
 begin:requests.filter(request=>request.route==='guided/calibration/begin'),
 status:elements.get('htCalibrationAxisStatus').textContent}));
})();
""")
    assert result["supported"]
    assert result["initiallyDisabled"]
    assert result["unconfirmedWrites"] == []
    assert result["supportRequired"]
    assert result["confirmedEnabled"]
    assert result["begin"] == [{"route": "guided/calibration/begin", "body": {
        "release_confirmed": True, "z_supported": True,
    }}]
    assert "已松轴" in result["status"]


def test_four_read_buttons_keep_working_z_independent_and_require_all_four_to_lock():
    result = evaluate_session("""
(async()=>{
 HighThroughput.activate();await tick();
 if(!elements.has('htCaptureWorking')){console.log(JSON.stringify({supported:false}));return;}
 confirmRelease();await click('htCalibrationBegin');
 for(const id of ['htCaptureFirst','htCaptureLast','htCaptureSafe'])await click(id);
 const beforeWorking=elements.get('htCalibrationFinish').disabled;
 await click('htCalibrationFinish');
 const premature=requests.filter(request=>request.route==='guided/calibration/finish').length;
 await click('htCaptureWorking');
 const complete=!elements.get('htCalibrationFinish').disabled;
 await click('htCalibrationFinish');
 console.log(JSON.stringify({supported:true,beforeWorking,premature,complete,
 captures:requests.filter(request=>request.route==='guided/calibration/capture').map(request=>request.body),
 summary:elements.get('htCalibrationSummary').textContent,status:elements.get('htCalibrationAxisStatus').textContent}));
})();
""")
    assert result["supported"]
    assert result["beforeWorking"]
    assert result["premature"] == 0
    assert result["complete"]
    assert result["captures"] == [
        {"point": "first"}, {"point": "last"}, {"point": "safe"}, {"point": "working"},
    ]
    assert "四点" in result["summary"]
    assert "已锁轴" in result["status"]


def test_trusted_four_points_run_without_separate_absolute_limits_but_legacy_three_cannot():
    result = evaluate_session("""
(async()=>{
 sessionGuided.calibration=completeCalibration();controllerSnapshot.motion.axes={X:sessionAxis(true),Z:sessionAxis(true)};
 limitsReadFailure=true;
 HighThroughput.activate();await tick();
 elements.get('htMode').value='live';change('htMode');elements.get('htLiveConfirmed').checked=true;change('htLiveConfirmed');
 const currentEnabled=!elements.get('htRun').disabled;
 delete sessionGuided.calibration.points.working;sessionGuided.calibration.four_point=false;
 limitsReadFailure=false;limitsSnapshot={...limitsSnapshot,state:'active',active:true};
 HighThroughput.activate();await tick();
 console.log(JSON.stringify({currentEnabled,legacyBlocked:elements.get('htRun').disabled}));
})();
""")
    assert result == {"currentEnabled": True, "legacyBlocked": True}


def test_relock_target_mismatch_refreshes_blocked_state_without_claiming_ready():
    result = evaluate_session("""
(async()=>{
 HighThroughput.activate();await tick();
 if(!elements.has('htCaptureWorking')){console.log(JSON.stringify({supported:false}));return;}
 confirmRelease();await click('htCalibrationBegin');
 for(const id of ['htCaptureFirst','htCaptureLast','htCaptureWorking','htCaptureSafe'])await click(id);
 finishFailure=true;await click('htCalibrationFinish');
 console.log(JSON.stringify({supported:true,blocked:elements.get('htRun').disabled,
 progress:elements.get('htCalibrationProgress').textContent,
 status:elements.get('htCalibrationAxisStatus').textContent,
 forbidden:requests.filter(request=>/motion\\/(enable|move|reference)/.test(request.route)),
 error:elements.get('htError').textContent}));
})();
""")
    assert result["supported"]
    assert result["blocked"]
    assert "原位锁轴受阻" in result["progress"]
    assert "目标" in result["progress"]
    assert "已松轴" in result["status"]
    assert "已锁轴" not in result["status"]
    assert result["forbidden"] == []
    assert "目标" in result["error"]
    assert "manual_relock_target" not in result["error"]


def test_serial_link_and_stale_feedback_never_claim_axes_locked_or_allow_live():
    result = evaluate_session("""
(async()=>{
 sessionGuided.calibration=completeCalibration();controllerSnapshot.motion.axes={};
 HighThroughput.activate();await tick();
 if(!elements.has('htCalibrationAxisStatus')){console.log(JSON.stringify({supported:false}));return;}
 elements.get('htMode').value='live';change('htMode');elements.get('htLiveConfirmed').checked=true;change('htLiveConfirmed');
 const link={status:elements.get('htCalibrationAxisStatus').textContent,blocked:elements.get('htRun').disabled};
 controllerSnapshot.motion.axes={X:sessionAxis(true),Z:sessionAxis(true)};
 HighThroughput.activate();await tick();
 HighThroughput.acceptDevices({available:true,received_at_ms:Date.now()-4000,snapshot:{motion:controllerSnapshot.motion}});
 const stale={status:elements.get('htCalibrationAxisStatus').textContent,blocked:elements.get('htRun').disabled};
 console.log(JSON.stringify({supported:true,link,stale}));
})();
""")
    assert result["supported"]
    for phase in ("link", "stale"):
        assert result[phase]["blocked"]
        assert "状态未确认" in result[phase]["status"]
        assert "已锁轴" not in result[phase]["status"]


def test_ordinary_page_keeps_legacy_calibration_tools_hidden():
    result = evaluate_session("""
(async()=>{
 HighThroughput.activate();await tick();
 console.log(JSON.stringify({legacyHidden:elements.get('htLegacyCalibration')?.hidden===true,
  supported:elements.has('htCaptureWorking'),writes:requests.filter(request=>request.body!==null)}));
})();
""")
    assert result == {"legacyHidden": True, "supported": True, "writes": []}


def test_current_session_four_points_can_start_live_while_limits_are_unavailable():
    result = evaluate_session("""
(async()=>{
 experiment.safety={motion_policy_confirmed:true,electrical_policy_confirmed:true};
 sessionGuided.calibration=completeCalibration();controllerSnapshot.motion.axes={X:sessionAxis(true),Z:sessionAxis(true)};
 limitsReadFailure=true;
 HighThroughput.activate();await tick();
 elements.get('htMode').value='live';change('htMode');elements.get('htLiveConfirmed').checked=true;change('htLiveConfirmed');
 await click('htRun');
 console.log(JSON.stringify({runs:requests.filter(request=>request.route==='guided/run').map(request=>({mode:request.body.mode,plan_hash:request.body.plan_hash})),
  legacyWrites:requests.filter(request=>request.body!==null&&/^(limits|twin)\\//.test(request.route)),
  error:elements.get('htError').hidden?null:elements.get('htError').textContent}));
})();
""")
    assert result == {"runs": [{"mode": "live", "plan_hash": "four-point-session-plan"}],
                      "legacyWrites": [], "error": None}


def test_reconnected_bus_revokes_current_session_run_even_if_four_point_record_says_usable():
    result = evaluate_session("""
(async()=>{
 sessionGuided.calibration=completeCalibration();controllerSnapshot.motion.axes={X:sessionAxis(true),Z:sessionAxis(true)};
 HighThroughput.activate();await tick();
 elements.get('htMode').value='live';change('htMode');elements.get('htLiveConfirmed').checked=true;change('htLiveConfirmed');
 const before=!elements.get('htRun').disabled;
 controllerSnapshot.motion.connection_token='new-connection';HighThroughput.activate();await tick();
 await click('htRun');
 console.log(JSON.stringify({before,blocked:elements.get('htRun').disabled,
  runs:requests.filter(request=>request.route==='guided/run'),summary:elements.get('htCalibrationSummary').textContent}));
})();
""")
    assert result["before"]
    assert result["blocked"]
    assert result["runs"] == []
    assert "需完成当前连接四点标定" in result["summary"]


def test_cached_axis_without_feedback_age_cannot_certify_current_lock_state():
    result = evaluate_session("""
(async()=>{
 sessionGuided.calibration=completeCalibration();controllerSnapshot.motion.axes={X:sessionAxis(true),Z:sessionAxis(true)};
 HighThroughput.activate();await tick();
 elements.get('htMode').value='live';change('htMode');elements.get('htLiveConfirmed').checked=true;change('htLiveConfirmed');
 const cached=JSON.parse(JSON.stringify(controllerSnapshot.motion));
 delete cached.axes.X.feedback_age_s;delete cached.axes.Z.feedback_age_s;
 HighThroughput.acceptDevices({available:true,received_at_ms:Date.now(),snapshot:{motion:cached}});
 console.log(JSON.stringify({blocked:elements.get('htRun').disabled,status:elements.get('htCalibrationAxisStatus').textContent}));
})();
""")
    assert result["blocked"]
    assert "状态未确认" in result["status"]
    assert "已锁轴" not in result["status"]


def test_production_device_cache_requires_explicit_trusted_calibration_readiness():
    result = evaluate_session("""
(async()=>{
 sessionGuided.calibration=completeCalibration();controllerSnapshot.motion.axes={X:sessionAxis(true),Z:sessionAxis(true)};
 HighThroughput.activate();await tick();
 elements.get('htMode').value='live';change('htMode');elements.get('htLiveConfirmed').checked=true;change('htLiveConfirmed');
 const withoutCache=!elements.get('htRun').disabled;
 const cache={available:true,received_at_ms:Date.now(),snapshot:{motion:controllerSnapshot.motion}};
 HighThroughput.acceptDevices(cache);const missingBlocked=elements.get('htRun').disabled;
 cache.snapshot.calibration={required:true,ready:false,state:'stale'};
 HighThroughput.acceptDevices(cache);const rejectedBlocked=elements.get('htRun').disabled;
 cache.snapshot.calibration={required:true,ready:true,state:'ready'};
 HighThroughput.acceptDevices(cache);const trustedEnabled=!elements.get('htRun').disabled;
 console.log(JSON.stringify({withoutCache,missingBlocked,rejectedBlocked,trustedEnabled}));
})();
""")
    assert result == {"withoutCache": True, "missingBlocked": True,
                      "rejectedBlocked": True, "trustedEnabled": True}
