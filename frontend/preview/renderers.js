// Business groups register only fully implemented renderers. No placeholder
// page is counted as finished by the preview review catalogue.
import {renderDashboardPreview,renderWellDetailPreview} from './presenters/dashboard.js';
import {renderWellList} from '../pages/wells.js';
import {renderMonitoring} from './pages/monitoring.js';

export const renderers={dashboard:renderDashboardPreview,wells:renderWellList,'well-detail':renderWellDetailPreview,
  ...Object.fromEntries(['production-monitor','well-realtime','well-history','well-status','fluid-level','haulage-monitor'].map(id=>[id,renderMonitoring]))};
