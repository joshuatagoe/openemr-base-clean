// Allergies card model. PHP: templates/patient/card/allergies.html.twig
// ("<title> (<severity>)", severity highlighted when severe / life threatening /
// fatal, reaction in the tooltip, no sorting). FHIR mapping: A2 §2.3.
import type { AllergyIntolerance, CodeableConcept } from 'fhir/r4';

export interface AllergyRow {
  id: string;
  name: string;
  /** The FHIR criticality label ('' when absent). OpenEMR squashes the PHP severity into it (gap G4). */
  severity: string;
  /** PHP highlights severe / life threatening / fatal; FHIR `high` is the closest (also covers "moderate to severe"). */
  highlight: boolean;
  /** PHP: `<title> Reaction: <reaction> - <severity>`. */
  tooltip: string;
}

// The labels OpenEMR's FhirAllergyIntoleranceService uses for each criticality.
// `unable-to-assess` comes only from the PHP severity "Unassigned", so the PHP
// word is shown for it; low and high each stand for several PHP severities.
const CRITICALITY_LABELS: Readonly<Record<string, string>> = {
  low: 'Low Risk',
  high: 'High Risk',
  'unable-to-assess': 'Unassigned',
};

/**
 * Plain text of an XHTML narrative. OpenEMR inserts `lists.title` into the div
 * without escaping, so the div is parsed as inert HTML and only its text is
 * used; it is never rendered as markup.
 */
export function narrativeText(div: string | undefined): string {
  if (!div) return '';
  const doc = new DOMParser().parseFromString(div, 'text/html');
  return (doc.body.textContent ?? '').replace(/\s+/g, ' ').trim();
}

export function conceptText(concept: CodeableConcept | undefined): string {
  if (!concept) return '';
  if (concept.text?.trim()) return concept.text.trim();
  const coding = concept.coding?.find((c) => c.display?.trim() && c.system !== 'http://terminology.hl7.org/CodeSystem/data-absent-reason');
  return coding?.display?.trim() ?? '';
}

function statusCode(concept: CodeableConcept | undefined): string | undefined {
  return concept?.coding?.[0]?.code;
}

/**
 * PHP shows issues whose outcome is not "Resolved" and whose end date is empty or
 * in the future. OpenEMR's FHIR maps "no end date" to `active`, "resolved with
 * an end date" to `resolved`, and any other end date to `inactive` (it does not
 * emit the date), so only `active` is kept. A future end date is the one case
 * the PHP card still shows and this card does not (documented).
 */
function isShown(a: AllergyIntolerance): boolean {
  if (statusCode(a.verificationStatus) === 'entered-in-error') return false;
  const clinical = statusCode(a.clinicalStatus);
  return clinical === undefined || clinical === 'active';
}

export function allergyRows(resources: readonly AllergyIntolerance[]): AllergyRow[] {
  return resources.filter(isShown).map((a, i) => {
    const name = narrativeText(a.text?.div) || conceptText(a.code) || 'Unnamed allergy';
    const severity = a.criticality ? (CRITICALITY_LABELS[a.criticality] ?? a.criticality) : '';
    const reaction = a.reaction?.[0]?.manifestation?.map(conceptText).filter(Boolean).join(', ') ?? '';
    return {
      id: a.id ?? `allergy-${i}`,
      name,
      severity,
      highlight: a.criticality === 'high',
      tooltip: `${name} Reaction: ${reaction} - ${severity}`,
    };
  });
}
