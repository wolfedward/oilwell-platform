from backend.inversion.result_codec import POINT_COLUMNS,EVALUATION_COLUMNS
from backend.inversion.result_codec_v2 import PROOF_COLUMNS
from tests.test_inversion_browser import BrowserFixtureCase


def rows(refused=False):
    output=[]
    for kind,columns in (('prediction',POINT_COLUMNS),('comparison',EVALUATION_COLUMNS),('proof',PROOF_COLUMNS)):
        for i in range(1025):
            invalid=refused or i==10
            row={k:None for k in columns};row.update(time_ns=str(1790784000000000000+i*7812500),power_index=str(i))
            if kind=='prediction':row.update(s_m='1/3' if i==0 else str(i/512),load_N=None if invalid else '2000/3',status='input_gap' if invalid else 'valid',
                reason_codes=['invalid_power_quality'] if invalid else [],error_bound_N=None if invalid else '1/10000000',power_W='2000',theta_rad='1',drive_Nm='100')
            elif kind=='comparison':row.update(valid=not invalid,reason_codes=['invalid_power_quality'] if invalid else [],load_value_si='700',displacement_value_si=str(i/512),
                load_indices=[str(i),str(i)],displacement_indices=[str(i),str(i)],load_weights=['1','0'],displacement_weights=['1','0'],
                load_source=0,displacement_source=0,reference_edge_from_previous=i>0)
            else:row.update(status='refused' if invalid else 'valid',reason_codes=['invalid_power_quality'] if invalid else [],
                angle_indices=[str(n) for n in range(7)],motion_status='valid',motion_reason_codes=[],exact_center=['1','1','0'],base=['0','1/10000000','0'],
                physical=['0','0','0'],numerical=['0','0','0'],**{k:None if invalid else '1/10000000' for k in PROOF_COLUMNS[-5:]})
            output.append({'kind':kind,'columns':list(columns),'values':[row[k] for k in columns]})
    return output


class InversionV2BrowserTests(BrowserFixtureCase):
    fixture='inversion_v2_results_check.html'

    def test_v2_native_proof_toggles_hover_and_full_pagination(self):
        for width in (1120,760):self.run_fixture('v2-proof',width,encoded_rows=rows())

    def test_v2_refusal_does_not_fabricate_prediction_or_hide_reference(self):
        for width in (1120,760):self.run_fixture('v2-refused',width,encoded_rows=rows(True))

    def test_v2_report_separates_rejection_assertion_from_approved_execution(self):
        from tools.testpack.report import project_inversion_outcome
        from tools.testpack.inversion_http_v2 import InversionOutcome
        from tests.test_testpack_report_v2 import positive
        values=[project_inversion_outcome(positive()),project_inversion_outcome(InversionOutcome('guard-cancel','passed','refused','refused','worker','user_cancelled','refused',{}))]
        self.run_fixture('v2-report',760,encoded_rows=values)

    def test_retained_reader_dual_traces_native_gaps_and_last_proof_page(self):
        for width in (1120,760):self.run_fixture('retained-reader',width,encoded_rows=rows())
