import axios from 'axios'
// This is a local UI observation, never a scientific verification claim.
export const recordCitationView = (conversationId:string,revisionId:string,locatorId:string) =>
  axios.post('/api/onboarding/citation-visit',{conversation_id:conversationId,revision_id:revisionId,locator_id:locatorId})
