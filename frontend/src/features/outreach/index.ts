// Outreach feature: email drafting/sending per option, an Outreach panel (emails + Luma), and the
// runners for the email steps (judges, invitations, follow-up).
import { registerResultAction } from '../../lib/resultActions'
import EmailAction from './EmailAction'
import { mountOutreachPanel } from './mount'
import { registerOutreachRunners } from './runner'

registerResultAction({
  id: 'email-draft',
  // Rendered for sample data too (as a disabled button) so the option still shows where emailing will live.
  applies: (_task, result) => Boolean(result.draft || result.contact?.email || result.emailId),
  Component: EmailAction,
})

registerOutreachRunners()
mountOutreachPanel()

export { default as OutreachPanel } from './OutreachPanel'
export { setOutreachEventId } from './location'
