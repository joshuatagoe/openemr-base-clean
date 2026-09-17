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
        $bundle = $this->builder()->build(self::CID, self::PATIENT, $this->note(), [$this->labRow()], null, [], []);

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
            'order_id' => null,
            'test_name' => 'Hemoglobin A1c',
            'code' => null,
            'value' => '8.9',
            'units' => '%',
            'abnormal_flag' => 'high',
            'range' => null,
            'status' => 'final',
            'observed_at' => '2026-09-12T09:15:00Z',
        ]], $bundle['lab_results']);
        self::assertSame([], $bundle['lab_orders']);
    }

    public function testOrdersAndResultLinksAreMapped(): void
    {
        // Orders map one row per ordered test; results keep their order link, code and range verbatim.
        $lab = $this->labRow() + ['order_id' => 12, 'code' => '4548-4', 'range' => '4.0-5.6'];
        $orders = [
            ['order_id' => 12, 'seq' => 1, 'test_name' => 'Hemoglobin A1c', 'code' => '4548-4', 'order_status' => 'complete', 'ordered_at' => '2026-09-10 08:00:00'],
            ['order_id' => 13, 'seq' => 2, 'test_name' => 'Lipid Panel', 'code' => '', 'order_status' => 'weird', 'ordered_at' => '2026-09-14 08:00:00'],
            ['order_id' => 14, 'seq' => 1, 'test_name' => '', 'code' => '', 'order_status' => 'pending', 'ordered_at' => '2026-09-14 08:00:00'],
            ['order_id' => 15, 'seq' => 1, 'test_name' => 'TSH', 'code' => '', 'order_status' => 'pending', 'ordered_at' => '0000-00-00 00:00:00'],
        ];
        $builder = $this->builder();
        $bundle = $builder->build(self::CID, self::PATIENT, $this->note(), [$lab], null, $orders, []);
        self::assertSame('procedure_order:12', $bundle['lab_results'][0]['order_id']);
        self::assertSame('4548-4', $bundle['lab_results'][0]['code']);
        self::assertSame('4.0-5.6', $bundle['lab_results'][0]['range']);
        self::assertSame([
            ['order_id' => 'procedure_order:12', 'sequence' => 1, 'test_name' => 'Hemoglobin A1c', 'code' => '4548-4', 'status' => 'complete', 'ordered_at' => '2026-09-10T13:00:00Z'],
            ['order_id' => 'procedure_order:13', 'sequence' => 2, 'test_name' => 'Lipid Panel', 'code' => null, 'status' => 'unknown', 'ordered_at' => '2026-09-14T13:00:00Z'],
        ], $bundle['lab_orders']);
        self::assertSame(2, $builder->getOmittedCounts()['orders_omitted']);
        self::assertSame([], $bundle['data_quality']['sources_unavailable']);
    }

    public function testMedicationStatusIsDerivedPerDataQualityRules(): void
    {
        // AUDIT DATA-003: active iff flag=1 AND (end empty OR end > now); disagreement -> indeterminate (null).
        $now = '2026-09-17 10:00:00';
        $rows = [
            ['source_table' => 'prescriptions', 'id' => 31, 'drug' => 'Metformin 500 mg', 'rxnorm' => '861007', 'dosage' => '1 tab BID', 'active' => 1, 'begdate' => '2026-01-15', 'enddate' => '', 'date_added' => '2026-01-15 09:00:00', 'date_modified' => '2026-06-12 08:00:00'],
            ['source_table' => 'lists', 'id' => 9, 'drug' => 'Lisinopril', 'rxnorm' => '', 'dosage' => '', 'active' => 1, 'begdate' => '2025-02-01 00:00:00', 'enddate' => '2026-03-01 00:00:00', 'date_added' => '2025-02-01 00:00:00', 'date_modified' => ''],
            ['source_table' => 'prescriptions', 'id' => 40, 'drug' => 'Atorvastatin 20 mg', 'rxnorm' => '', 'dosage' => '', 'active' => 0, 'begdate' => '', 'enddate' => '2027-01-01', 'date_added' => '2026-06-11 09:00:00', 'date_modified' => ''],
            ['source_table' => 'lists', 'id' => 10, 'drug' => 'Amlodipine', 'rxnorm' => '', 'dosage' => '', 'active' => 0, 'begdate' => '2024-01-01 00:00:00', 'enddate' => '2024-06-01 00:00:00', 'date_added' => '2024-01-01 00:00:00', 'date_modified' => ''],
            ['source_table' => 'prescriptions', 'id' => 41, 'drug' => '', 'rxnorm' => '', 'dosage' => '', 'active' => 1, 'begdate' => '', 'enddate' => '', 'date_added' => '2026-01-01 00:00:00', 'date_modified' => ''],
            ['source_table' => 'other', 'id' => 42, 'drug' => 'X', 'rxnorm' => '', 'dosage' => '', 'active' => 1, 'begdate' => '', 'enddate' => '', 'date_added' => '2026-01-01 00:00:00', 'date_modified' => ''],
        ];
        $builder = $this->builder();
        $bundle = $builder->build(self::CID, self::PATIENT, $this->note(), [], null, [], $rows, $now);
        $meds = $bundle['medications'];
        self::assertCount(4, $meds);
        self::assertSame(2, $builder->getOmittedCounts()['medications_omitted']);

        self::assertSame('prescriptions:31', $meds[0]['record_id']);
        self::assertTrue($meds[0]['active']);
        self::assertSame('861007', $meds[0]['rxnorm_code']);
        self::assertSame('1 tab BID', $meds[0]['dosage_text']);
        self::assertSame('2026-01-15T06:00:00Z', $meds[0]['started_at']); // bare DATE -> local midnight (CST) -> UTC
        self::assertSame('2026-06-12T13:00:00Z', $meds[0]['modified_at']);
        self::assertSame('2026-06-12T13:00:00Z', $meds[0]['timestamp']);
        self::assertSame('date_modified', $meds[0]['timestamp_field']);
        self::assertSame('active,end_date', $meds[0]['status_field']);
        self::assertSame('active=1,end_date=null', $meds[0]['status_value']);

        self::assertSame('lists:9', $meds[1]['record_id']);
        self::assertNull($meds[1]['active']); // flag active but ended in the past -> indeterminate
        self::assertSame('activity,enddate', $meds[1]['status_field']);

        self::assertSame('prescriptions:40', $meds[2]['record_id']);
        self::assertNull($meds[2]['active']); // flag inactive but end date in the future -> indeterminate

        self::assertSame('lists:10', $meds[3]['record_id']);
        self::assertFalse($meds[3]['active']);
        self::assertSame([], $bundle['data_quality']['sources_unavailable']);
    }

    public function testDuplicateMedicationRowsCollapseWithACount(): void
    {
        // AUDIT DATA-004: identical rows (same source, drug, start, status) collapse; a differing status does not.
        $row = ['source_table' => 'lists', 'id' => 1, 'drug' => 'Metformin', 'rxnorm' => '', 'dosage' => '', 'active' => 1, 'begdate' => '2026-01-15 00:00:00', 'enddate' => '', 'date_added' => '2026-01-15 00:00:00', 'date_modified' => ''];
        $rows = [$row, ['id' => 2, 'drug' => 'METFORMIN'] + $row, ['id' => 3, 'active' => 0, 'enddate' => '2026-03-01 00:00:00'] + $row, ['id' => 4, 'source_table' => 'prescriptions'] + $row];
        $bundle = $this->builder()->build(self::CID, self::PATIENT, $this->note(), [], null, [], $rows, '2026-09-17 10:00:00');
        self::assertSame(['lists:1', 'lists:3', 'prescriptions:4'], array_column($bundle['medications'], 'record_id'));
        self::assertSame(1, $bundle['data_quality']['duplicates_collapsed']);
    }

    public function testUnavailableMedicationsSourceIsDeclaredNotEmptied(): void
    {
        $bundle = $this->builder()->build(self::CID, self::PATIENT, $this->note(), [], null, [], null);
        self::assertSame([], $bundle['medications']);
        self::assertSame(['medications'], $bundle['data_quality']['sources_unavailable']);
    }

    public function testUnavailableOrdersSourceIsDeclaredNotEmptied(): void
    {
        $bundle = $this->builder()->build(self::CID, self::PATIENT, $this->note(), [], null, null, []);
        self::assertSame([], $bundle['lab_orders']);
        self::assertSame(['lab_orders'], $bundle['data_quality']['sources_unavailable']);
    }

    public function testNoResultsIsAnEmptyAvailableList(): void
    {
        // Boundary: checked and empty -> [] with no unavailable sources (the agent reports no_matching_record_found).
        $bundle = $this->builder()->build(self::CID, self::PATIENT, $this->note(), [], null, [], []);
        self::assertSame([], $bundle['lab_results']);
        self::assertSame([], $bundle['data_quality']['sources_unavailable']);
    }

    public function testUnavailableLabSourceIsDeclaredNotEmptied(): void
    {
        // Guards: a failed lab read is declared so the agent returns verification_unavailable, never "nothing found".
        $bundle = $this->builder()->build(self::CID, self::PATIENT, $this->note(), null, null, [], []);
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
            'orders_omitted' => 0,
            'medications_omitted' => 0,
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
