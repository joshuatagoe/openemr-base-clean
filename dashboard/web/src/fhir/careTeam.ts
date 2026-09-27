// Care Team card model. PHP: CareTeamViewCard + manage_care_team.html.twig
// (one team: the first active one, else the first; name + status badge; members
// with Type / Member / Role / Facility / Since / Status / Note).
// FHIR mapping and gaps: A2 §2.5 (G9–G12).
import type { CareTeam, CareTeamParticipant } from 'fhir/r4';
import { conceptText } from './allergy';

export interface CareTeamMemberRow {
  key: string;
  type: 'provider' | 'related';
  memberId: string;
  role: string;
  /** Organization id from `onBehalfOf` (the member's facility). */
  facilityId: string | undefined;
  /** `period.start` = provider_since, raw YYYY-MM-DD like the PHP card. */
  since: string;
}

export interface CareTeamViewModel {
  name: string;
  status: string;
  statusLabel: string;
  badgeClass: string;
  members: CareTeamMemberRow[];
}

const BADGES: Readonly<Record<string, string>> = {
  active: 'badge-success',
  inactive: 'badge-warning',
  proposed: 'badge-info',
  'entered-in-error': 'badge-danger',
};

// Titles of OpenEMR's Care_Team_Status list.
const STATUS_LABELS: Readonly<Record<string, string>> = {
  active: 'Active',
  inactive: 'Inactive',
  proposed: 'Proposed',
  suspended: 'Suspended',
  'entered-in-error': 'Entered in Error',
};

/** PHP CareTeamService::getCareTeamData: skip entered-in-error, first active team, else the first. */
export function pickCareTeam(teams: readonly CareTeam[]): CareTeam | undefined {
  const usable = teams.filter((t) => t.status !== 'entered-in-error');
  return usable.find((t) => t.status === 'active') ?? usable[0];
}

function refParts(reference: string | undefined): { type: string; id: string } | undefined {
  const m = /(?:^|\/)(Practitioner|RelatedPerson|Organization|Patient)\/([A-Za-z0-9.-]{1,64})$/.exec(reference ?? '');
  return m ? { type: m[1] as string, id: m[2] as string } : undefined;
}

export function organizationId(reference: string | undefined): string | undefined {
  const parts = refParts(reference);
  return parts?.type === 'Organization' ? parts.id : undefined;
}

function member(p: CareTeamParticipant, index: number): CareTeamMemberRow | undefined {
  const ref = refParts(p.member?.reference);
  // OpenEMR adds each member's facility again as an Organization participant;
  // those are not people, and the facility is already on the member's row.
  if (!ref || (ref.type !== 'Practitioner' && ref.type !== 'RelatedPerson')) return undefined;
  return {
    key: `${ref.type}/${ref.id}#${index}`,
    type: ref.type === 'Practitioner' ? 'provider' : 'related',
    memberId: ref.id,
    role: (p.role ?? []).map(conceptText).find(Boolean) ?? '',
    facilityId: ref.type === 'Practitioner' ? organizationId(p.onBehalfOf?.reference) : undefined,
    since: p.period?.start ? p.period.start.slice(0, 10) : '',
  };
}

export function careTeamView(team: CareTeam): CareTeamViewModel {
  const status = team.status ?? '';
  return {
    name: team.name ?? '',
    status,
    statusLabel: STATUS_LABELS[status] ?? status,
    badgeClass: BADGES[status] ?? 'badge-secondary',
    members: (team.participant ?? []).map(member).filter((m): m is CareTeamMemberRow => m !== undefined),
  };
}
