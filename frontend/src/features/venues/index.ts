// Venue feature: the `venue` step runs the backend venue-research agents (one board card per venue).
// Other google-maps steps (catering, hotels, …) stay on the mock: /venue-searches is venue-specific.
import { registerRunner } from '../../lib/runners'
import { runVenueSearch } from './runner'

registerRunner('venue', runVenueSearch)
