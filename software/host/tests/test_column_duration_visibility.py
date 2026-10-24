import pytest

from test_automation_gui import evaluate_browser


BOOTSTRAP = """
for(const column of experiment.columns)column.duration_s=column.index===1?5:180;
"""


def test_column_choices_show_each_actual_duration_without_saving_or_changing_parameters():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();
console.log(JSON.stringify({buttons:[1,2,12].map(index=>({
 text:elements.get('htColumnEdit-'+index).textContent,title:elements.get('htColumnEdit-'+index).title,
 label:elements.get('htColumnEdit-'+index).attributes['aria-label']})),
 writes:requests.filter(request=>request.body!==null),
 values:experiment.columns.map(column=>column.duration_s)}));})();
""", before_source=BOOTSTRAP)
    for button, duration in zip(result["buttons"], [5, 180, 180]):
        assert f"{duration} s" in button["text"]
        assert f"{duration} s" in button["title"]
        assert f"{duration} s" in button["label"]
    assert result["writes"] == []
    assert result["values"] == [5] + [180] * 11


@pytest.mark.parametrize("column,duration", [(1, 5), (2, 180)])
def test_electrode_window_heading_is_explicitly_scoped_to_selected_column(column, duration):
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();await click('htColumnEdit-COLUMN');
console.log(JSON.stringify({heading:elements.get('htWindowHeading')?.textContent,
 scope:elements.get('htWindowScope')?.textContent,
 duration:elements.get('htColumnDuration').value,
 windows:'ABCDEFGH'.split('').map(id=>elements.get('htWindow-'+id).placeholder),
 writes:requests.filter(request=>request.body!==null)}));})();
""".replace("COLUMN", str(column)), before_source=BOOTSTRAP)
    assert f"第 {column} 列" in result["heading"]
    assert "其他列" in result["scope"]
    assert result["duration"] == str(duration)
    assert result["windows"] == [str(duration)] * 8
    assert result["writes"] == []
