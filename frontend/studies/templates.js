// Deliberate, labelled software vectors. These are never loaded until the example button is pressed.
const iso = (day, minute = 0) => `2026-01-${String(day).padStart(2, '0')}T${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}:00+00:00`;

export function examplePayload(kind, dependencyId = '') {
  if (kind === 'condition_training') {
    const points = [['A', -2, 0], ['A', -1.8, .1], ['B', 2, 0], ['B', 1.8, -.1], ['C', 0, 2], ['C', .1, 1.8]];
    return {schema: 'condition.training/1', features: [{name: 'x', unit: 'm'}, {name: 'y', unit: 'm'}, {name: 'constant', unit: 'm'}],
      rows: ['train', 'validation', 'test'].flatMap((partition, day) => points.map(([label, x, y], index) => ({
        sample_id: `${partition}-${index}`, well_id: `${partition}-well-${Math.floor(index / 2)}`, cycle_id: `${partition}-cycle-${index}`,
        observed_at: iso(day + 1, index), partition, label, values: [x + day * .02, y + day * .02, 10],
      }))),
      rules: [{label: 'A', clauses: [{feature: 'x', op: 'le', value: -1}]}, {label: 'B', clauses: [{feature: 'x', op: 'ge', value: 1}]}, {label: 'C', clauses: [{feature: 'y', op: 'ge', value: 1}]}],
      options: {seed: 7, hidden_units: 5, epochs: 80, learning_rate: .2, l2: .001, confidence_min: .5, ood_margin: .2}};
  }
  if (kind === 'condition_prediction') return {schema: 'condition.prediction/1', model_record_id: dependencyId,
    rows: [{sample_id: 'new-1', well_id: 'new-well', cycle_id: 'new-cycle', observed_at: iso(4), values: [-1.9, .05, 10]}],
    confidence_min: .5, ood_margin: .2};
  if (kind === 'recovery_fit') {
    const minutes = [0, 30, 60, 90, 120, 180, 240, 300];
    return {schema: 'recovery.depth/1', well_id: 'SOFTWARE-W-1', datum: '软件样例基准面', unit: 'm', shutdown_at: iso(1), resolution_m: .01, accuracy_m: .1,
      policy: {tau_min_minutes: 20, tau_max_minutes: 500, tau_grid_points: 256, min_r_squared: .99, max_rmse_m: 1, max_gap_minutes: 60, max_relative_tau_spread: .3, max_equilibrium_spread_m: 5},
      observations: minutes.map(t => ({at: iso(1, t), depth_m: 100 + 40 * Math.exp(-t / 120), quality: 'valid', pump_state: 'off'}))};
  }
  if (kind === 'energy_schedule') return {schema: 'energy.schedule/1', horizon_start: '2026-01-01T00:00:00+08:00', horizon_end: '2026-01-02T00:00:00+08:00', production_day_timezone: 'Asia/Shanghai', site_power_limit_kw: 20, max_simultaneous_starts: 2,
    wells: [{well_id: 'SOFTWARE-W-1', recovery_record_id: dependencyId, parameter_basis: '软件示例参数，需替换为有来源的现场资料', initial_depth_m: 120, initial_state: 'off', initial_state_elapsed_minutes: 60, annulus_storage_area_m2: 1, pump_intake_depth_m: 200, minimum_submergence_m: 1, pumping_liquid_rate_m3_h: 6, run_input_power_kw: 4, standby_input_power_kw: 1, startup_input_power_kw: 8, startup_duration_minutes: 30, min_on_minutes: 60, min_off_minutes: 60, max_starts_per_calendar_day: 8, minimum_liquid_m3_per_calendar_day: 1, terminal_max_depth_m: 150,
      candidates: [{candidate_id: 'bounded-cycle', segments: Array.from({length: 8}, () => [{state: 'on', duration_minutes: 120}, {state: 'off', duration_minutes: 60}]).flat()},
        {candidate_id: 'unsupported-long-shut-in', segments: [{state: 'off', duration_minutes: 60}, {state: 'on', duration_minutes: 120}, {state: 'off', duration_minutes: 1260}]}]}]};
  if (kind === 'energy_effects') {
    const period = (day, energy) => ({start: `2026-01-0${day}T00:00:00+08:00`, end: `2026-01-0${day}T02:00:00+08:00`, intervals: [{start: `2026-01-0${day}T00:00:00+08:00`, end: `2026-01-0${day}T01:00:00+08:00`, liquid_m3: 2, energy_kwh: energy, quality: 'valid'}]});
    return {schema: 'energy.effects/1', well_id: 'SOFTWARE-W-1', baseline: period(1, 4), treatment: period(2, 3), min_coverage_fraction: .5,
      comparability: {same_meter: true, consistent_units: true, similar_operating_conditions: true, other_changes_recorded: true}};
  }
  if (kind === 'motor_spectrum') {
    const n = 2048, fs = 1024;
    return {schema: 'motor.spectrum/1', capture: {capture_id: 'software-capture', well_id: 'SOFTWARE-W-1', device_id: 'software-device', started_at: '2026-10-11T00:00:00+00:00', sample_rate_hz: fs,
      samples_A: Array.from({length: n}, (_, i) => 7 + 3 * Math.sin(2 * Math.PI * 50 * i / fs)), quality: Array(n).fill('valid'), clipped: Array(n).fill(false),
      provenance: {sample_kind: 'instantaneous', uniform_sampling_confirmed: true, clock_error_ppm: 10, calibration_reference: '软件样例', anti_alias_reference: '软件样例', anti_alias_passband_hz: 400, manifest_sha256: 'a'.repeat(64)}},
      motor: {type: 'squirrel_cage_induction', supply: 'line_fed', pole_count: 4, speed_rpm: 1440, speed_uncertainty_rpm: 0, supply_frequency_hz: 50, frequency_uncertainty_hz: 0, steady_state_declared: true, periodic_load_excluded: true, basis_reference: '软件样例'},
      policy: {maximum_clock_error_ppm: 100, maximum_rms_variation_ratio: .1, minimum_fundamental_A: 1, minimum_sideband_dbc: -35, minimum_sideband_to_noise_db: 6}};
  }
  if (kind === 'protection_replay') {
    const at = ms => `2026-10-11T00:00:${String(Math.floor(ms / 1000)).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}+00:00`;
    return {schema: 'motor.protection/1', settings: {version: 'software-example/1', basis_reference: '软件样例阈值', approved_by: '软件样例作者', effective_from: '2026-10-11T00:00:00+00:00', effective_until: '2026-10-12T00:00:00+00:00', maximum_frame_gap_ms: 2000, reset_safe_ms: 500,
      rules: [{id: 'overcurrent', metric: 'current_max_A', direction: 'high', pickup: 10, dropout: 8, delay_ms: 1000}]},
      frames: [0, 400, 1000, 1400].map((ms, index) => ({at: at(ms), sequence: index + 1, boot_id: 'software-boot', quality: 'valid', configuration_valid: true, hardware_trip: false, reset_request: 'none', measurements: {currents_A: [index ? 11 : 10, index ? 11 : 10, index ? 11 : 10], voltages_V: null, temperature_C: null, speed_rpm: null, frequency_hz: null}}))};
  }
  throw Error(`Unsupported study kind: ${kind}`);
}
