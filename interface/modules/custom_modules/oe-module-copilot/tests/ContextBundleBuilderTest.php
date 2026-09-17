<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use InvalidArgumentException;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Support\UtcDate;
use PHPUnit\Framework\TestCase;

/**
 * Pure transformation tests: OpenEMR rows in, contract-shaped array out.
 * Each test names the failure mode it guards against.
 */
final class ContextBundleBuilderTest extends TestCase
{
    private const CID = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
    private const PATIENT = ['pid' => 42, 'uuid' => '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e'];

    private function builder(string $tz = 'America/Chicago'): ContextBundleBuilder
    {
        return new ContextBundleBuilder(new DateTimeZone($tz));
    }

    /** @return array{form_soap_id:int, encounter:int, note_date:string, plan:string} */
    private function note(string $plan = 'Continue metformin. Repeat HbA1c in three months.', int $id = 1001): array
    {
        return ['form_soap_id' => $id, 'encounter' => 501, 'note_date' => '2026-06-10 09:30:00', 'plan' => $plan];
    }

    /**
     * @param array<string,mixed> $overrides
     * @return array<string,mixed>
     */
    private function labRow(array $overrides = []): array
    {
        return array_merge([
            'result_id' => 9001,
            'test_name' => 'Hemoglobin A1c',
            'value' => '8.9',
            'units' => '%',
            'abnormal' => 'high',
            'result_status' => 'final',
            'observed_at' => '2026-09-12 04:15:00',
        ], $overrides);
    }

    public function testUtcConversionFromLocalZone(): void
    {
        // Guards: local datetimes must become UTC, not merely be relabelled.
        $chicago = new DateTimeZone('America/Chicago'); // CDT in June/September = UTC-5
        self::assertSame('2026-06-10T14:30:00Z', UtcDate::toIso('2026-06-10 09:30:00', $chicago));
        self::assertSame('2026-09-12T09:15:00Z', UtcDate::toIso('2026-09-12 04:15:00', $chicago));
        self::assertSame('2026-06-10T05:00:00Z', UtcDate::toIso('2026-06-10', $chicago)); // DATE = local midnight
        self::assertSame('2026-06-10T14:30:00Z', UtcDate::toIso('2026-06-10 14:30:00', new DateTimeZone('UTC')));
    }

    public function testZeroAndEmptyDatesAreRejected(): void
    {
        // Guards: OpenEMR zero dates must not silently become the epoch.
        $this->expectException(InvalidArgumentException::class);
        UtcDate::toIso('0000-00-00 00:00:00', new DateTimeZone('UTC'));
    }

    public function testTracerBulletBundleShape(): void
    {
        // Tracer bullet: fixture-equivalent bundle with stable ids, UTC dates, mapped status and flag.
        $bundle = $this->builder()->build(self::CID, self::PATIENT, $this->note(), [$this->labRow()]);

        self::assertSame('1.0', $bundle['schema_version']);
        self::assertSame(self::CID, $bundle['correlation_id']);
        self::assertSame(self::PATIENT['uuid'], $bundle['patient_uuid']);
        self::assertSame([
            'note_id' => 'form_soap:1001',
            'encounter_id' => 'form_encounter:501',
            'note_date' => '2026-06-10T14:30:00Z',
            'plan_text' => 'Continue metformin. Repeat HbA1c in three months.',
        ], $bundle['prior_note']);
        self::assertSame(['sources_unavailable' => [], 'duplicates_collapsed' => 0], $bundle['data_quality']);
        self::assertSame([[
            'result_id' => 'procedure_result:9001',
            'test_name' => 'Hemoglobin A1c',
            'value' => '8.9',
            'units' => '%',
            'abnormal_flag' => 'high',
            'status' => 'final',
            'observed_at' => '2026-09-12T09:15:00Z',
        ]], $bundle['lab_results']);
    }

    public function testNoResultsIsAnEmptyAvailableList(): void
    {
        // Boundary: checked and empty -> [] with no unavailable sources (the agent reports no_matching_record_found).
        $bundle = $this->builder()->build(self::CID, self::PATIENT, $this->note(), []);
        self::assertSame([], $bundle['lab_results']);
        self::assertSame([], $bundle['data_quality']['sources_unavailable']);
    }

    public function testUnavailableLabSourceIsDeclaredNotEmptied(): void
    {
        // Guards: a failed lab read is declared so the agent returns verification_unavailable, never "nothing found".
        $bundle = $this->builder()->build(self::CID, self::PATIENT, $this->note(), null);
        self::assertSame([], $bundle['lab_results']);
        self::assertSame(['lab_results'], $bundle['data_quality']['sources_unavailable']);
    }

    public function testStatusAndFlagVocabularyMapping(): void
    {
        // Guards: OpenEMR option ids map to the contract enums; unrepresentable rows are omitted and counted, never guessed.
        $builder = $this->builder();
        $bundle = $builder->build(self::CID, self::PATIENT, $this->note(), [
            $this->labRow(['result_id' => 1, 'result_status' => 'prelim', 'abnormal' => '']),
            $this->labRow(['result_id' => 2, 'result_status' => 'correct', 'abnormal' => 'low']),
            $this->labRow(['result_id' => 3, 'result_status' => 'incomplete', 'abnormal' => 'vhigh']),
            $this->labRow(['result_id' => 4, 'result_status' => 'cancel']),
            $this->labRow(['result_id' => 5, 'result_status' => '']),
            $this->labRow(['result_id' => 6, 'value' => 'DNR']),
            $this->labRow(['result_id' => 7, 'units' => '']),
            $this->labRow(['result_id' => 8, 'test_name' => '  ']),
            $this->labRow(['result_id' => 9, 'observed_at' => '0000-00-00 00:00:00']),
            $this->labRow(['result_id' => 10, 'abnormal' => 'vlow']),
            $this->labRow(['result_id' => 11, 'abnormal' => 'critical']),
        ]);

        $byId = [];
        foreach ($bundle['lab_results'] as $r) {
            $id = $r['result_id'] ?? null;
            self::assertIsString($id);
            $byId[$id] = $r;
        }
        self::assertSame(
            ['procedure_result:1', 'procedure_result:2', 'procedure_result:3', 'procedure_result:7', 'procedure_result:10', 'procedure_result:11'],
            array_keys($byId)
        );
        self::assertSame('preliminary', $byId['procedure_result:1']['status']);
        self::assertNull($byId['procedure_result:1']['abnormal_flag']);
        self::assertSame('corrected', $byId['procedure_result:2']['status']);
        self::assertSame('low', $byId['procedure_result:2']['abnormal_flag']);
        self::assertSame('incomplete', $byId['procedure_result:3']['status']);
        self::assertSame('high', $byId['procedure_result:3']['abnormal_flag']); // vhigh collapses to high
        self::assertSame('low', $byId['procedure_result:10']['abnormal_flag']); // vlow collapses to low
        self::assertNull($byId['procedure_result:11']['abnormal_flag']); // unknown flag -> null, counted
        self::assertNull($byId['procedure_result:7']['units']);
        self::assertSame([
            'empty_test_name' => 1,
            'non_numeric_value' => 1,
            'unmapped_status' => 2,
            'unmapped_abnormal_flag' => 1,
            'bad_timestamp' => 1,
        ], $builder->getOmittedCounts());
    }

    public function testInputsAreNotMutated(): void
    {
        $patient = self::PATIENT;
        $note = $this->note();
        $labs = [$this->labRow()];
        $before = [$patient, $note, $labs];
        $this->builder()->build(self::CID, $patient, $note, $labs);
        self::assertSame($before, [$patient, $note, $labs]);
    }
}
