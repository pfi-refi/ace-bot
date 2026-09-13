/* NIGEL prototype — fictional data only.
   Every name, household, carrier, balance and reference number here is invented.
   Nothing in this file is connected to a live system. */
window.NIGEL_DATA = (function () {
  // Systems on the cloud. Labels follow the reference; Paraclete 1 is the annuity flagship.
  const systems = [
    { id: 'people', name: 'People', tagline: 'Team and partners', pos: [0.50, 0.14], posPortrait: [0.74, 0.18],
      sectors: [
        { id: 'team', name: 'Team', count: 5, note: 'Praxis staff and roles', records: [] },
        { id: 'partners', name: 'Partners', count: 14, note: 'Carriers, custodians, attorneys', records: [] },
        { id: 'coi', name: 'Centers of influence', count: 9, note: 'Referral relationships', records: [] }
      ] },
    { id: 'clients', name: 'Clients', tagline: 'Households and prospects', pos: [0.335, 0.18], posPortrait: [0.26, 0.18],
      sectors: [
        { id: 'households', name: 'Households', count: 61, note: 'Active relationships', records: [] },
        { id: 'prospects', name: 'Prospects', count: 12, note: 'In conversation', records: [] },
        { id: 'milestones', name: 'Milestones', count: 4, note: 'Birthdays and anniversaries this month', records: [] }
      ] },
    { id: 'knowledge', name: 'Knowledge', tagline: 'Playbooks and guides', pos: [0.76, 0.20], posPortrait: [0.74, 0.31],
      sectors: [
        { id: 'playbooks', name: 'Playbooks', count: 18, note: 'How Praxis does things', records: [] },
        { id: 'carriers', name: 'Carrier guides', count: 22, note: 'Product and underwriting notes', records: [] },
        { id: 'notes', name: 'Notes', count: 140, note: 'Captured from meetings', records: [] }
      ] },
    { id: 'investments', name: 'Investments', tagline: 'Managed assets', pos: [0.255, 0.42], posPortrait: [0.26, 0.31],
      sectors: [
        { id: 'aum', name: 'AUM', count: 2, note: 'Model drift and billing reviews', records: [], attention: true },
        { id: 'households', name: 'Households', count: 61, note: 'Advisory relationships', records: [] },
        { id: 'rebalancing', name: 'Rebalancing', count: 5, note: 'Queued for next trade window', records: [] },
        { id: 'fees', name: 'Fee schedules', count: 4, note: 'Tiered and flat schedules', records: [] }
      ] },
    { id: 'planning', name: 'Planning', tagline: 'Plans and scenarios', pos: [0.845, 0.39], posPortrait: [0.74, 0.45],
      sectors: [
        { id: 'plans', name: 'Financial plans', count: 33, note: 'Current plans on file', records: [] },
        { id: 'due', name: 'Reviews due', count: 6, note: 'Annual plan reviews in the next 60 days', records: [] },
        { id: 'scenarios', name: 'Scenarios', count: 9, note: 'What-if models in progress', records: [] }
      ] },
    { id: 'paraclete', name: 'Paraclete 1', tagline: 'Annuity pipeline', pos: [0.45, 0.60], posPortrait: [0.26, 0.59],
      sectors: [
        { id: 'pending', name: 'Pending annuity clients', count: 4, note: 'Cases between intake and carrier issue', records: ['okonkwo-reyes', 'thornbury', 'halvorsen', 'adeyemi-park'] },
        { id: 'active', name: 'Active contracts', count: 27, note: 'Issued and in force', records: [] },
        { id: 'submissions', name: 'Carrier submissions', count: 3, note: 'Transmitted, awaiting acknowledgement', records: [] },
        { id: 'illustrations', name: 'Illustrations', count: 9, note: 'Generated in the last 30 days', records: [] }
      ] },
    { id: 'servicing', name: 'Servicing', tagline: 'Policies and service', pos: [0.755, 0.60], posPortrait: [0.74, 0.59],
      sectors: [
        { id: 'insurance', name: 'Insurance', count: 1, note: 'Lapse and underwriting flags', records: [], attention: true },
        { id: 'policies', name: 'Policies', count: 38, note: 'Term, whole and universal life', records: [] },
        { id: 'requests', name: 'Service requests', count: 7, note: 'Open client requests', records: [] },
        { id: 'renewals', name: 'Renewals', count: 6, note: 'Next 90 days', records: [] }
      ] },
    { id: 'pipeline', name: 'Pipeline', tagline: 'Opportunities', pos: [0.32, 0.62], posPortrait: [0.26, 0.45],
      sectors: [
        { id: 'opportunities', name: 'Opportunities', count: 15, note: 'Qualified and in motion', records: [] },
        { id: 'proposals', name: 'Proposals', count: 6, note: 'Sent, awaiting a decision', records: [] },
        { id: 'followups', name: 'Follow-ups', count: 3, note: 'Promised call-backs', records: [] }
      ] },
    { id: 'godpod', name: 'GodPod', tagline: 'Faith and reflection', pos: [0.615, 0.73], posPortrait: [0.74, 0.72],
      sectors: [
        { id: 'reading', name: 'Morning reading', count: 1, note: 'Today\'s passage', records: [] },
        { id: 'prayer', name: 'Prayer list', count: 11, note: 'People and needs', records: [] },
        { id: 'journal', name: 'Journal', count: 40, note: 'Entries this year', records: [] }
      ] }
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
      id: 'rv-aum-1', system: 'investments', sector: 'aum', kind: 'Model drift',
      title: 'Drift beyond band — Thornbury Family Trust',
      summary: 'Equity sleeve is 68% against a 60% target. The 5-point band was crossed on Sep 10.',
      detail: 'Proposed rebalance sells $41,200 of the domestic equity sleeve into short-duration bonds. Estimated realized gain $3,900. Trust has no wash-sale exposure in the window.',
      due: 'Sep 16', severity: 'amber', status: 'open'
    },
    {
      id: 'rv-aum-2', system: 'investments', sector: 'aum', kind: 'Billing check',
      title: 'Fee schedule mismatch — Halvorsen IRA',
      summary: 'Account is billed at 1.10% but the household schedule says 0.95% above $500k combined.',
      detail: 'Combined household assets crossed $500,000 on Aug 31. The tier change did not propagate to the IRA. Proposed fix: apply the 0.95% tier and issue a $62 credit for the partial period.',
      due: 'Sep 15', severity: 'amber', status: 'open'
    },
    {
      id: 'rv-ins-1', system: 'servicing', sector: 'insurance', kind: 'Lapse warning',
      title: 'Premium lapse notice — Vasquez term life',
      summary: 'Quarterly premium of $412 was not received. Grace period ends Sep 27.',
      detail: 'Autopay failed twice on a closed card. Client was emailed on Sep 8 with no reply. Proposed action: call today and offer to move autopay to the joint checking account on file.',
      due: 'Sep 27', severity: 'amber', status: 'open'
    }
  ];

  const activity = [
    { when: 'Today 08:40', what: 'Morning sweep found 3 items that need attention.', system: 'nigel' },
    { when: 'Yesterday', what: 'Adeyemi-Park case submitted to carrier (simulated).', system: 'paraclete' },
    { when: 'Sep 10', what: 'Thornbury drift crossed the 5-point band.', system: 'investments' },
    { when: 'Sep 8', what: 'Lapse notice emailed to Vasquez household.', system: 'servicing' }
  ];

  return { systems, households, reviews, activity };
})();
