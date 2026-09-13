/* NIGEL prototype — fictional data only.
   Every name, household, carrier, balance and reference number here is invented.
   Nothing in this file is connected to a live system. */
window.NIGEL_DATA = (function () {
  const systems = [
    {
      id: 'paraclete', name: 'Paraclete 1', tagline: 'Annuity pipeline',
      pos: [0.32, 0.30], posPortrait: [0.25, 0.16],
      sectors: [
        { id: 'pending', name: 'Pending annuity clients', count: 4, note: 'Cases between intake and carrier issue', records: ['okonkwo-reyes', 'thornbury', 'halvorsen', 'adeyemi-park'] },
        { id: 'active', name: 'Active contracts', count: 27, note: 'Issued and in force', records: [] },
        { id: 'submissions', name: 'Carrier submissions', count: 3, note: 'Transmitted, awaiting acknowledgement', records: [] },
        { id: 'illustrations', name: 'Illustrations', count: 9, note: 'Generated in the last 30 days', records: [] }
      ]
    },
    {
      id: 'aum', name: 'AUM', tagline: 'Managed assets',
      pos: [0.70, 0.27], posPortrait: [0.75, 0.16],
      sectors: [
        { id: 'reviews', name: 'Reviews', count: 2, note: 'Model drift and billing checks', records: [], attention: true },
        { id: 'households', name: 'Households', count: 61, note: 'Advisory relationships', records: [] },
        { id: 'rebalancing', name: 'Rebalancing', count: 5, note: 'Queued for next trade window', records: [] },
        { id: 'fees', name: 'Fee schedules', count: 4, note: 'Tiered and flat schedules', records: [] }
      ]
    },
    {
      id: 'insurance', name: 'Insurance', tagline: 'Policies in force',
      pos: [0.74, 0.64], posPortrait: [0.75, 0.33],
      sectors: [
        { id: 'reviews', name: 'Reviews', count: 1, note: 'Lapse and underwriting flags', records: [], attention: true },
        { id: 'policies', name: 'Policies', count: 38, note: 'Term, whole and universal life', records: [] },
        { id: 'underwriting', name: 'Underwriting', count: 2, note: 'Awaiting carrier decision', records: [] },
        { id: 'renewals', name: 'Renewals', count: 6, note: 'Next 90 days', records: [] }
      ]
    },
    {
      id: 'compliance', name: 'Compliance', tagline: 'Attestations and audit',
      pos: [0.30, 0.70], posPortrait: [0.25, 0.33],
      sectors: [
        { id: 'attestations', name: 'Attestations', count: 12, note: 'Annual and event-driven', records: [] },
        { id: 'audit', name: 'Audit log', count: 140, note: 'Actions recorded this quarter', records: [] }
      ]
    },
    {
      id: 'calendar', name: 'Calendar', tagline: 'Client meetings',
      pos: [0.585, 0.14], posPortrait: [0.75, 0.50],
      sectors: [
        { id: 'week', name: 'This week', count: 7, note: 'Confirmed appointments', records: [] },
        { id: 'followups', name: 'Follow-ups', count: 3, note: 'Promised call-backs', records: [] }
      ]
    },
    {
      id: 'ledger', name: 'Ledger', tagline: 'Revenue and commissions',
      pos: [0.62, 0.80], posPortrait: [0.25, 0.50],
      sectors: [
        { id: 'commissions', name: 'Commissions', count: 8, note: 'Expected this month', records: [] },
        { id: 'advisory', name: 'Advisory fees', count: 4, note: 'Quarterly billing runs', records: [] }
      ]
    },
    {
      id: 'signal', name: 'Signal', tagline: 'Client communications',
      pos: [0.88, 0.45], posPortrait: [0.75, 0.67],
      sectors: [
        { id: 'inbox', name: 'Inbox', count: 11, note: 'Unread client threads', records: [] },
        { id: 'campaigns', name: 'Campaigns', count: 2, note: 'Scheduled sends', records: [] }
      ]
    },
    {
      id: 'archive', name: 'Archive', tagline: 'Closed and historical',
      pos: [0.21, 0.51], posPortrait: [0.25, 0.67],
      sectors: [
        { id: 'closed', name: 'Closed cases', count: 212, note: 'Read-only', records: [] }
      ]
    }
  ];

  const households = {
    'okonkwo-reyes': {
      id: 'okonkwo-reyes', name: 'Okonkwo-Reyes Household',
      caseTitle: 'Fixed index annuity — 1035 exchange',
      stage: 1, // 0 intake, 1 suitability, 2 illustration, 3 submission, 4 issued
      members: [
        { name: 'Daniel Okonkwo', age: 58, role: 'Owner / annuitant' },
        { name: 'Marisol Reyes', age: 56, role: 'Joint owner' }
      ],
      contact: 'daniel.okonkwo@example.invalid · (555) 010-4471',
      advisor: 'Brady',
      riskProfile: 'Moderate — income focus',
      objective: 'Guaranteed lifetime income starting at 65; principal protection during the accumulation window.',
      sourceOfFunds: '1035 exchange from Meridian Life fixed annuity (fictional carrier). Surrender charge ends Nov 2026.',
      premium: 250000,
      proposedCarrier: 'Northwind Assurance (fictional)',
      product: 'Northwind Horizon FIA 10',
      documents: [
        { name: 'Intake questionnaire', status: 'Received', date: 'Sep 2' },
        { name: 'Existing contract statement', status: 'Received', date: 'Sep 4' },
        { name: 'Replacement disclosure', status: 'Needs signature', date: '—' },
        { name: 'Suitability memo', status: 'Not started', date: '—' }
      ],
      timeline: [
        { when: 'Sep 2', what: 'Intake completed with both owners present.' },
        { when: 'Sep 4', what: 'Meridian statement uploaded. Surrender schedule confirmed.' },
        { when: 'Sep 9', what: 'Case moved to suitability review.' }
      ]
    },
    'thornbury': {
      id: 'thornbury', name: 'Thornbury Family Trust', caseTitle: 'Multi-year guaranteed annuity — new money', stage: 2,
      members: [{ name: 'Eleanor Thornbury', age: 71, role: 'Trustee' }],
      contact: 'trust@example.invalid', advisor: 'Brady', riskProfile: 'Conservative',
      objective: 'Ladder a 5-year guaranteed rate against maturing CDs.',
      sourceOfFunds: 'Maturing certificates of deposit, $180,000.', premium: 180000,
      proposedCarrier: 'Harbor Mutual (fictional)', product: 'Harbor MYGA 5',
      documents: [{ name: 'Intake questionnaire', status: 'Received', date: 'Aug 20' }, { name: 'Suitability memo', status: 'Reviewed', date: 'Aug 28' }],
      timeline: [{ when: 'Aug 20', what: 'Intake completed.' }, { when: 'Aug 28', what: 'Suitability reviewed.' }]
    },
    'halvorsen': {
      id: 'halvorsen', name: 'Halvorsen Household', caseTitle: 'Single premium immediate annuity', stage: 0,
      members: [{ name: 'Per Halvorsen', age: 66, role: 'Owner' }, { name: 'Ingrid Halvorsen', age: 64, role: 'Joint annuitant' }],
      contact: 'halvorsen@example.invalid', advisor: 'Brady', riskProfile: 'Conservative — income now',
      objective: 'Cover the fixed-expense gap in retirement starting January.',
      sourceOfFunds: 'IRA rollover, $310,000.', premium: 310000,
      proposedCarrier: 'To be determined', product: 'Joint life SPIA, 10-year certain',
      documents: [{ name: 'Intake questionnaire', status: 'In progress', date: '—' }],
      timeline: [{ when: 'Sep 11', what: 'Discovery meeting held.' }]
    },
    'adeyemi-park': {
      id: 'adeyemi-park', name: 'Adeyemi-Park Household', caseTitle: 'Registered index-linked annuity', stage: 3,
      members: [{ name: 'Tunde Adeyemi', age: 49, role: 'Owner' }, { name: 'Soo-jin Park', age: 47, role: 'Beneficiary' }],
      contact: 'adeyemi.park@example.invalid', advisor: 'Brady', riskProfile: 'Growth with a buffer',
      objective: 'Equity participation with a 20% downside buffer over six years.',
      sourceOfFunds: 'Non-qualified brokerage transfer, $140,000.', premium: 140000,
      proposedCarrier: 'Coastline Financial (fictional)', product: 'Coastline Buffer 6',
      documents: [{ name: 'Suitability memo', status: 'Reviewed', date: 'Sep 1' }, { name: 'Illustration', status: 'Signed', date: 'Sep 6' }],
      timeline: [{ when: 'Sep 6', what: 'Illustration signed by both parties.' }, { when: 'Sep 8', what: 'Submitted to carrier (simulated). Awaiting acknowledgement.' }]
    }
  };

  const reviews = [
    {
      id: 'rv-aum-1', system: 'aum', sector: 'reviews', kind: 'Model drift',
      title: 'Drift beyond band — Thornbury Family Trust',
      summary: 'Equity sleeve is 68% against a 60% target. The 5-point band was crossed on Sep 10.',
      detail: 'Proposed rebalance sells $41,200 of the domestic equity sleeve into short-duration bonds. Estimated realized gain $3,900. Trust has no wash-sale exposure in the window.',
      due: 'Sep 16', severity: 'amber', status: 'open'
    },
    {
      id: 'rv-aum-2', system: 'aum', sector: 'reviews', kind: 'Billing check',
      title: 'Fee schedule mismatch — Halvorsen IRA',
      summary: 'Account is billed at 1.10% but the household schedule says 0.95% above $500k combined.',
      detail: 'Combined household assets crossed $500,000 on Aug 31. The tier change did not propagate to the IRA. Proposed fix: apply the 0.95% tier and issue a $62 credit for the partial period.',
      due: 'Sep 15', severity: 'amber', status: 'open'
    },
    {
      id: 'rv-ins-1', system: 'insurance', sector: 'reviews', kind: 'Lapse warning',
      title: 'Premium lapse notice — Vasquez term life',
      summary: 'Quarterly premium of $412 was not received. Grace period ends Sep 27.',
      detail: 'Autopay failed twice on a closed card. Client was emailed on Sep 8 with no reply. Proposed action: call today and offer to move autopay to the joint checking account on file.',
      due: 'Sep 27', severity: 'amber', status: 'open'
    }
  ];

  const activity = [
    { when: 'Today 08:40', what: 'Morning sweep found 3 items that need attention.', system: 'nigel' },
    { when: 'Yesterday', what: 'Adeyemi-Park case submitted to carrier (simulated).', system: 'paraclete' },
    { when: 'Sep 10', what: 'Thornbury drift crossed the 5-point band.', system: 'aum' },
    { when: 'Sep 8', what: 'Lapse notice emailed to Vasquez household.', system: 'insurance' }
  ];

  return { systems, households, reviews, activity };
})();
