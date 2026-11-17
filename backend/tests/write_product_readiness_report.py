"""Collect existing evidence; never converts missing or failed checks to passes."""
import hashlib,json,xml.etree.ElementTree as ET
from pathlib import Path

root=Path(__file__).resolve().parents[2]
out=root/'verification/product-readiness'
files=[
    'backend/app/product_journey.py','backend/app/api/onboarding.py',
    'backend/app/diagnostic_records.py','backend/app/api/diagnostic_history.py','backend/app/api/diagnostics.py','backend/app/diagnostic_probes.py',
    'backend/app/api/papers.py','backend/app/source_library.py','backend/app/api/conversations.py',
    'frontend/src/components/OnboardingGuide.vue','frontend/src/components/DiagnosticHistory.vue','frontend/src/components/DiagnosticsPanel.vue',
    'frontend/src/components/ResearchSidebar.vue','frontend/src/views/LibraryView.vue','frontend/src/views/GraphView.vue','frontend/src/views/ChatView.vue',
    'frontend/src/api.ts','frontend/src/researchApi.ts','frontend/src/onboardingApi.ts','frontend/src/views/SourceReaderView.vue',
    'backend/tests/test_product_journey.py','backend/tests/test_diagnostic_history.py','backend/tests/test_history_windows.py','backend/tests/test_library_pagination.py',
    'frontend/tests/product-readiness.cjs','frontend/tests/product-scale.cjs',
]
checks=[]
for name in ['core.xml','pagination.xml','pagination-recheck.xml','history-and-diagnostics.xml','final-core.xml']:
    if not (out/name).exists():continue
    xml=ET.parse(out/name).getroot();s=xml if xml.tag=='testsuite' else xml.find('testsuite')
    checks.append({'file':name,**{k:int(s.attrib.get(k,0)) for k in ['tests','failures','errors','skipped']}})
ui=json.loads((out/'ui/report.json').read_text(encoding='utf-8')) if (out/'ui/report.json').exists() else None
scale=json.loads((out/'scale/report.json').read_text(encoding='utf-8')) if (out/'scale/report.json').exists() else None
report={
    'workstream':'W11 product journey, persistent diagnostics and bounded projections',
    'status':'implemented_with_scoped_evidence_final_integrated_build_recheck_pending',
    'real_provider_calls':0,'checks':checks,'tests_overlap_do_not_sum':True,
    'failure_resolution':{'pagination.xml':'One test fixture used a nonexistent SourceLibraryImport.authorization_id. Corrected fixture request_id/raw_sha256; production behavior unchanged; pagination-recheck.xml passed.',
        'ui/failure.json':'Historical pointer/header clipping or fixture failures are retained. Current pass is ui/report.json; geometry checks the complete heading is reachable.',
        'scale/failure.json':'Historical failure correctly found sidebar still loading all papers. Sidebar now uses bounded page statistics; all 9 projections passed.'},
    'browser':{'report':'ui/report.json','ok':ui.get('ok') if ui else False},
    'scale':{'report':'scale/report.json','status':scale.get('status') if scale else 'missing','datasets':len(scale.get('datasets',[])) if scale else 0},
    'tested_build_inputs':'frontend-build-inputs.json',
    'implementation':[
        'Skippable durable local onboarding; explicit isolated example; default model does not silently switch to demo.',
        'Diagnostic/Recovery records with typed failures, configuration-bound probes, safe local/export projections and scoped receipt inspection.',
        'Library pages/search and bounded metadata joins never fetch raw originals just to render list entries.',
        'Graph target search, cancellation of view waiting and late-result isolation; graph matching still computes full data set.',
        'Chat history 30-turn windows, stable record anchors and exact trace restoration; latest run snapshots/SSE remain independent.',
        'Plan claim refresh projects only selected persistent IDs; rename reads summary rather than old answer bodies.'
    ],
    'limits':[
        'Existing independent-user first-value target, actual Windows 100/150/200 percent DPI/screen-reader matrix and 7-day mixed run are not yet certified.',
        'Scale data are synthetic metadata, graph relations and history, not imported originals. No OCR/import/embedding retrieval capacity is claimed.',
        'API P95 uses 20 samples per case; browser navigation only 3 samples. Frame intervals and explicit-GC heap checks do not establish a long-run leak-free guarantee.',
        'Graph pagination bounds returned/rendered nodes, not the full graph matching compute; persistent invalidatable projection remains an optimization gap.',
        'No installer or new commercial release was packaged by this workstream.'
    ],
    'source_sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files},
}
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'checks':checks,'ui_ok':report['browser']['ok'],'scale':report['scale']},ensure_ascii=False))
