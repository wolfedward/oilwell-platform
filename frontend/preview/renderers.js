// Business groups register only fully implemented renderers. No placeholder
// page is counted as finished by the preview review catalogue.
import {renderDashboardPreview,renderWellDetailPreview} from './presenters/dashboard.js';
import {renderWellList} from '../pages/wells.js';
import {renderMonitoring} from './pages/monitoring.js';
import {renderAcquisitionPreview} from './presenters/acquisition.js';
import {renderAcquisitionConfiguration} from './pages/acquisition.js';
import {renderDynagraphPreview} from './presenters/dynagraphs.js';
import {renderDynagraphWorkspace} from './pages/dynagraphs.js';
import {renderStudiesPreview} from './presenters/studies.js';
import {renderEnergyPreview} from './presenters/energy.js';
import {renderEnergyWorkspace} from './pages/energy.js';

export const renderers={dashboard:renderDashboardPreview,wells:renderWellList,'well-detail':renderWellDetailPreview,
  ...Object.fromEntries(['energy-overview','recovery-analysis','strategy-center','strategy-editor'].map(id=>[id,renderEnergyPreview])),
  'multiwell-schedule':renderEnergyWorkspace,'energy-effects':renderEnergyWorkspace,
  ...Object.fromEntries(['engineering-overview','condition-lab','recovery-lab','motor-lab','study-review'].map(id=>[id,renderStudiesPreview])),
  ...Object.fromEntries(['production-monitor','well-realtime','well-history','well-status','fluid-level','haulage-monitor'].map(id=>[id,renderMonitoring])),
  ...Object.fromEntries(['acquisition','data-quality','devices','field-registry','ingest-reliability','waveforms'].map(id=>[id,renderAcquisitionPreview])),
  'channel-mapping':renderAcquisitionConfiguration,'uplink-monitor':renderAcquisitionConfiguration,
  ...Object.fromEntries(['dynagraphs','dynagraph-analysis','diagnosis-review','production-parameters','production-inversion','production-results'].map(id=>[id,renderDynagraphPreview])),
  ...Object.fromEntries(['dynagraph-tracking','production-metering','card-alarm-rules','standard-cards'].map(id=>[id,renderDynagraphWorkspace]))};
