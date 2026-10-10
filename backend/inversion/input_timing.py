"""Absolute UTC support, distinct from pairwise numerical alignment.

All coordinates are exact Fractions. Mapping uncertainty is a bound, never a
second timestamp correction. The unused approved reference remains relevant.
"""
from fractions import Fraction
from backend.waveforms.pair_contracts import capture_key,public
from backend.waveforms.quality import channel_identity
from backend.waveforms.quality_context import utc_ns
from backend.waveforms.timing import sample_time_ns
from backend.waveforms.evidence_contracts import decimal_value
from .contracts import utc
from .parameters import covers_observation,refuse


def absolute_support(context,manifests):
    sync=context.sync_evidence
    if not sync or sync['proof']['status']!='resolved': refuse('absolute_timing_unknown')
    points=sync['proof']['endpoints']
    reference=next((p for p in points if p['mapping_role']=='reference'),None)
    if reference is None: refuse('absolute_timing_unknown')

    def bound(point,low,high):
        if any(point.get(k) is None for k in ('valid_start_ns','valid_end_ns','nominal_reference_ns',
                                              'offset_error_ns','residual_ns','frequency_error_ppm')):
            refuse('absolute_timing_unknown')
        if not int(point['valid_start_ns'])<=low<=high<int(point['valid_end_ns']): refuse('absolute_sync_interval')
        age=max(abs(low-Fraction(point['nominal_reference_ns'])),abs(high-Fraction(point['nominal_reference_ns'])))
        return Fraction(int(point['offset_error_ns'])+int(point['residual_ns']))+age*decimal_value(point['frequency_error_ppm'])/1000000

    ref_key=capture_key(reference['endpoint']['capture']); ref_frame=manifests.get(ref_key)
    if ref_frame is None or utc_ns(ref_frame) is None or ref_frame.body['utc_uncertainty_ns'] is None: refuse('absolute_timing_unknown')
    ref_stream=next((s for s in ref_frame.streams if s.stream_id==reference['endpoint']['stream_id']),None)
    if ref_stream is None: refuse('absolute_timing_unknown')
    ref_bound=bound(reference,sample_time_ns(ref_stream,ref_stream.first_sample_index,correction_ns=0),
                    sample_time_ns(ref_stream,ref_stream.first_sample_index+ref_stream.frame_count-1,correction_ns=0))
    terms=[]
    for data in context.streams:
        point=next((p for p in points if capture_key(p['endpoint']['capture'])==data.key
                    and p['endpoint']['stream_id']==data.stream.stream_id),None)
        if point is None: refuse('absolute_timing_unknown')
        own=bound(point,sample_time_ns(data.stream,data.first_index,correction_ns=0),
                  sample_time_ns(data.stream,data.last_index,correction_ns=0))
        frame=manifests[data.key]; anchor=utc_ns(frame)
        residual=context.calibrations[channel_identity(data)].body['residual_ns']
        if anchor is None or frame.body['utc_uncertainty_ns'] is None or residual is None: refuse('absolute_timing_unknown')
        additional=Fraction(0) if point['endpoint']==reference['endpoint'] else ref_bound
        reference_utc=0 if data.key==ref_key else int(ref_frame.body['utc_uncertainty_ns'])
        total=own+additional+int(frame.body['utc_uncertainty_ns'])+reference_utc+int(residual)
        start=anchor+sample_time_ns(data.stream,data.first_index,correction_ns=data.correction_ns)
        end=anchor+sample_time_ns(data.stream,data.last_index,correction_ns=data.correction_ns)
        body=sync['evidence'].body
        if body['valid_from'] is not None and not covers_observation(utc(body['valid_from']),utc(body['valid_to']) if body['valid_to'] else None,start,end,total):
            refuse('absolute_sync_interval')
        terms.append(dict(role=data.role,endpoint=public(point['endpoint']),endpoint_bound_ns=str(own),
            reference_endpoint=public(reference['endpoint']),reference_bound_ns=str(ref_bound),
            reference_additional_bound_ns=str(additional),reference_utc_uncertainty_ns=str(reference_utc),
            utc_uncertainty_ns=frame.body['utc_uncertainty_ns'],calibration_residual_ns=residual,total_bound_ns=str(total)))
    if not terms: refuse('absolute_timing_unknown')
    return max(Fraction(item['total_bound_ns']) for item in terms),terms
