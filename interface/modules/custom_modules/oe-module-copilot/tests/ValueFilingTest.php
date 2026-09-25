<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Controller\FilingController;
use OpenEMR\Modules\Copilot\Filing\FilingStoreInterface;
use OpenEMR\Modules\Copilot\Filing\ValueFiler;
use PHPUnit\Framework\TestCase;

/**
 * Verify and file / reject (ADR-003, ADR-009 §2, §4):
 *   POST /api/copilot/documents/{document_id}/values/{result_index}/file
 *   POST /api/copilot/documents/{document_id}/values/{result_index}/reject
 */
final class ValueFilingTest extends TestCase
{
    private const PID = 42;
    private const OTHER_PID = 77;
    private const USER = 1;
    private const DOC = 501;
    private const OTHER_DOC = 502;
    private const READ_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];
    private const EXTRACTION = '{"document_id":501,"results":[{"test_name":"Hemoglobin A1c","loinc_code":"4548-4"},{"test_name":"Glucose","loinc_code":null}]}';

    private CapturingLogger $logger;
    private AuditCapture $audit;
    private FakeFilingStore $store;
    private FakeFileSource $documents;

    protected function setUp(): void
    {
        $this->logger = new CapturingLogger();
        $this->audit = new AuditCapture();
        $this->store = new FakeFilingStore();
        $this->store->documents[self::DOC] = ['pid' => self::PID, 'status' => 'extracted', 'doc_type' => 'lab_pdf', 'extraction_json' => self::EXTRACTION];
        $this->store->documents[self::OTHER_DOC] = ['pid' => self::OTHER_PID, 'status' => 'extracted', 'doc_type' => 'lab_pdf', 'extraction_json' => '{"results":[]}'];
        $this->documents = new FakeFileSource(docs: [
            self::DOC => ['pid' => self::PID, 'media_type' => 'application/pdf', 'size' => 1000],
            self::OTHER_DOC => ['pid' => self::OTHER_PID, 'media_type' => 'application/pdf', 'size' => 1000],
        ]);
    }

    /**
     * @param array<string,bool> $acl
     * @param array<string,bool> $write
     */
    private function controller(array $acl = self::READ_ACL + ['patients/sign' => true], array $write = ['patients/lab' => true], bool $ready = true, array $bases = [self::USER . ':' . self::PID => 'primary_provider']): FilingController
    {
        $read = new FakeAcl($acl);
        return new FilingController(
            new CopilotAuthorizer($read, new FakeRelationships($bases)),
            $read,
            new FakeWriteAcl($write),
            new FakeSchemaStatus($ready),
            $this->documents,
            new ValueFiler($this->store),
            $this->logger,
            $this->audit,
        );
    }

    private static function session(?int $user = self::USER): array
    {
        return ['authUserID' => $user, 'authUser' => $user === null ? null : 'dr_smith', 'pid' => self::PID];
    }

    /** @param array<string,mixed>|null $body */
    private function file(int $index = 0, ?array $body = ['filed_value' => null, 'confirm_unverified' => false], int $doc = self::DOC, ?FilingController $c = null): array
    {
        return ($c ?? $this->controller())->fileForSession(self::session(), $doc, $index, $body);
    }

    private function candidate(int $id): array
    {
        return $this->store->candidates[$id];
    }

    private function assertNothingWritten(): void
    {
        self::assertSame([], $this->store->orders);
        self::assertSame([], $this->store->reports);
        self::assertSame([], $this->store->results);
    }

    private function assertNoPhi(): void
    {
        $all = json_encode($this->audit->events, JSON_THROW_ON_ERROR) . $this->logger->dump();
        foreach (['Hemoglobin', 'Glucose', '7.1', '142'] as $needle) {
            self::assertStringNotContainsString($needle, $all);
        }
    }

    // ------------------------------------------------------------------ //
    // Filing chain
    // ------------------------------------------------------------------ //

    public function testFilingAVerifiedValueWritesTheOutsideLabChain(): void
    {
        $cid = $this->store->addCandidate(self::DOC, self::PID, 0);

        $result = $this->file();

        self::assertSame(200, $result['status'], json_encode($result['body']));
        $body = $result['body'];
        self::assertSame('filed', $body['status']);
        self::assertFalse($body['already_filed']);
        self::assertSame('final', $body['result_status']);
        self::assertNull($body['warning']);

        self::assertCount(1, $this->store->labs);
        self::assertSame([FilingStoreInterface::OUTSIDE_LAB_NAME], array_values($this->store->labs));
        self::assertCount(1, $this->store->orders);
        $order = array_values($this->store->orders)[0];
        self::assertSame(self::PID, $order['pid']);
        self::assertSame(self::USER, $order['provider_id']);
        self::assertSame(array_key_first($this->store->labs), $order['lab_id']);
        self::assertSame('2026-09-01 00:00:00', $order['date']);
        self::assertCount(1, $this->store->reports);

        self::assertCount(1, $this->store->results);
        $resultId = array_key_first($this->store->results);
        $row = $this->store->results[$resultId];
        self::assertSame(array_key_first($this->store->reports), $row['procedure_report_id']);
        self::assertSame('N', $row['result_data_type']);
        self::assertSame('4548-4', $row['result_code']);
        self::assertSame('Hemoglobin A1c', $row['result_text']);
        self::assertSame('2026-09-01 00:00:00', $row['date']);
        self::assertSame('%', $row['units']);
        self::assertSame('7.1', $row['result']);
        self::assertSame('4.0-5.6', $row['range']);
        self::assertSame('high', $row['abnormal']);
        self::assertStringContainsString('7.1', $row['comments']);
        self::assertSame(0, $row['document_id'], 'ADR-009 7b: no core document link, it hides the value in the order-results screen');
        self::assertSame('final', $row['result_status']);

        $c = $this->candidate($cid);
        self::assertSame('filed', $c['status']);
        self::assertSame(self::USER, $c['filed_by']);
        self::assertSame('7.1', $c['filed_value']);
        self::assertSame($resultId, $c['procedure_result_id']);
        self::assertSame($resultId, $body['procedure_result_id']);

        self::assertCount(1, $this->audit->events);
        $event = $this->audit->events[0];
        self::assertSame(FilingController::AUDIT_FILED, $event['event']);
        self::assertTrue($event['success']);
        self::assertStringContainsString('document_id=' . self::DOC, $event['comment']);
        self::assertStringContainsString('result_index=0', $event['comment']);
        self::assertStringContainsString('outcome=filed', $event['comment']);
        self::assertStringContainsString('procedure_result_id=' . $resultId, $event['comment']);
        $this->assertNoPhi();
    }

    public function testASecondValueReusesTheDocumentsOrderAndReport(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $this->store->addCandidate(self::DOC, self::PID, 1, ['test_name' => 'Glucose', 'value_text' => '142', 'unit' => 'mg/dL', 'reference_range' => '70-99']);

        self::assertSame(200, $this->file(0)['status']);
        self::assertSame(200, $this->file(1)['status']);

        self::assertCount(1, $this->store->labs);
        self::assertCount(1, $this->store->orders);
        self::assertCount(1, $this->store->reports);
        self::assertCount(2, $this->store->results);
        $second = array_values($this->store->results)[1];
        self::assertSame('', $second['result_code'], 'no LOINC code in the extraction -> empty, never guessed');
    }

    public function testASecondClickReturnsTheExistingResult(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);

        $first = $this->file();
        $second = $this->file();

        self::assertSame(200, $second['status']);
        self::assertTrue($second['body']['already_filed']);
        self::assertSame($first['body']['procedure_result_id'], $second['body']['procedure_result_id']);
        self::assertCount(1, $this->store->results);
        self::assertCount(1, $this->store->orders);
        self::assertStringContainsString('outcome=already_filed', $this->audit->events[1]['comment']);
    }

    public function testEveryWriteHappensInOneTransactionAndAFailureLeavesNothing(): void
    {
        $cid = $this->store->addCandidate(self::DOC, self::PID, 0);
        $this->store->failAt = 'insertResult';

        $result = $this->file();

        self::assertSame(500, $result['status']);
        self::assertSame('filing_failed', $result['body']['detail']['code']);
        $this->assertNothingWritten();
        self::assertSame([], $this->store->labs);
        self::assertSame('candidate', $this->candidate($cid)['status']);
        self::assertFalse($this->audit->events[0]['success']);
        self::assertStringNotContainsString('deadlock', $this->logger->dump() . json_encode($result['body']) . $this->audit->events[0]['comment']);

        $this->store->failAt = 'markFiled';
        self::assertSame(500, $this->file()['status']);
        $this->assertNothingWritten();

        $this->store->failAt = null;
        self::assertSame(200, $this->file()['status']);
        self::assertCount(1, $this->store->results);
    }

    public function testAClinicianCorrectionIsFiledAsCorrectedAndKeepsTheExtractedValue(): void
    {
        $cid = $this->store->addCandidate(self::DOC, self::PID, 0);

        $result = $this->file(0, ['filed_value' => ' 7.4 ', 'confirm_unverified' => false]);

        self::assertSame('corrected', $result['body']['result_status']);
        $row = array_values($this->store->results)[0];
        self::assertSame('7.4', $row['result']);
        self::assertSame('corrected', $row['result_status']);
        self::assertStringContainsString('7.1', $row['comments']);
        self::assertSame('7.4', $this->candidate($cid)['filed_value']);
    }

    public function testTheSameValueTypedBackIsNotACorrection(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $result = $this->file(0, ['filed_value' => '7.1', 'confirm_unverified' => false]);
        self::assertSame('final', $result['body']['result_status']);
    }

    public function testADerivedFlagIsNotFiledAsAbnormal(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0, ['flag_source' => 'derived']);
        $this->file();
        self::assertSame('', array_values($this->store->results)[0]['abnormal']);
    }

    public function testPrintedFlagsMapToOpenEmrOptionIds(): void
    {
        $index = 10;
        foreach (['H', 'L', 'HH', 'LL', 'A', 'N'] as $flag) {
            $this->store->addCandidate(self::DOC, self::PID, $index++, ['abnormal_flag' => $flag]);
        }
        foreach ($this->store->candidates as $c) {
            $this->file($c['result_index']);
        }
        $flags = array_column(array_values($this->store->results), 'abnormal');
        sort($flags);
        self::assertSame(['high', 'low', 'no', 'vhigh', 'vlow', 'yes'], $flags);
    }

    public function testANonNumericValueIsFiledAsString(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0, ['value_text' => 'Negative', 'unit' => null, 'reference_range' => null, 'abnormal_flag' => null]);
        $this->file();
        $row = array_values($this->store->results)[0];
        self::assertSame('S', $row['result_data_type']);
        self::assertSame('', $row['units']);
        self::assertSame('', $row['abnormal']);
    }

    public function testTheSameResultAlreadyInTheChartWarnsButFiles(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $this->store->chart[] = ['pid' => self::PID, 'test_name' => 'hemoglobin a1c', 'code' => '', 'date' => '2026-09-01', 'value' => '7.1', 'status' => 'final'];

        $result = $this->file();

        self::assertSame(200, $result['status']);
        self::assertSame('same_result_already_in_chart', $result['body']['warning']);
        self::assertCount(1, $this->store->results);
        self::assertStringContainsString('warning=same_result_already_in_chart', $this->audit->events[0]['comment']);
    }

    // ------------------------------------------------------------------ //
    // Verification-status rules
    // ------------------------------------------------------------------ //

    public function testAnUnverifiedValueNeedsExplicitConfirmation(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0, ['verification_status' => 'unverified']);

        $refused = $this->file();
        self::assertSame(422, $refused['status']);
        self::assertSame('confirmation_required', $refused['body']['detail']['code']);
        $this->assertNothingWritten();

        $filed = $this->file(0, ['filed_value' => null, 'confirm_unverified' => true]);
        self::assertSame(200, $filed['status']);
    }

    public function testAFuzzyVerifiedValueFilesWithoutExtraConfirmation(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0, ['verification_status' => 'verified_fuzzy']);
        self::assertSame(200, $this->file()['status']);
    }

    public function testAnUnreadableValueNeedsAClinicianEnteredValue(): void
    {
        $cid = $this->store->addCandidate(self::DOC, self::PID, 0, ['verification_status' => 'unreadable', 'value_text' => null]);

        foreach ([null, '', '   '] as $value) {
            $refused = $this->file(0, ['filed_value' => $value, 'confirm_unverified' => true]);
            self::assertSame(422, $refused['status']);
            self::assertSame('value_required', $refused['body']['detail']['code']);
        }
        $this->assertNothingWritten();

        $filed = $this->file(0, ['filed_value' => '6.9', 'confirm_unverified' => false]);
        self::assertSame(200, $filed['status']);
        self::assertSame('corrected', $filed['body']['result_status']);
        $row = array_values($this->store->results)[0];
        self::assertSame('6.9', $row['result']);
        self::assertStringContainsString('unreadable', $row['comments']);
        self::assertSame('6.9', $this->candidate($cid)['filed_value']);
    }

    public function testAValueWithoutACollectionDateIsNotFiled(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0, ['collection_date' => null]);
        $result = $this->file();
        self::assertSame(422, $result['status']);
        self::assertSame('collection_date_missing', $result['body']['detail']['code']);
        $this->assertNothingWritten();
    }

    // ------------------------------------------------------------------ //
    // Ownership, state and permissions
    // ------------------------------------------------------------------ //

    public function testAnotherPatientsValueIs404AndNothingIsWritten(): void
    {
        $this->store->addCandidate(self::OTHER_DOC, self::OTHER_PID, 0);
        $result = $this->file(0, doc: self::OTHER_DOC);
        self::assertSame(404, $result['status']);
        self::assertSame('value_not_found', $result['body']['detail']['code']);
        $this->assertNothingWritten();
    }

    public function testACandidateRecordedUnderAnotherPatientIs404EvenIfTheDocumentMoved(): void
    {
        // The OpenEMR document now belongs to this patient but our candidate row is the old patient's.
        $this->store->documents[self::DOC]['pid'] = self::OTHER_PID;
        $this->store->addCandidate(self::DOC, self::OTHER_PID, 0);
        self::assertSame(404, $this->file()['status']);
        $this->assertNothingWritten();
    }

    public function testMissingValueAndMissingDocumentGiveTheSame404(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $missingIndex = $this->file(9);
        $missingDoc = $this->file(0, doc: 99999);
        self::assertSame(404, $missingIndex['status']);
        self::assertSame(404, $missingDoc['status']);
        self::assertSame($missingIndex['body']['detail']['message'], $missingDoc['body']['detail']['message']);
    }

    public function testCanAccessFalseIs403(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $this->documents->denied = [self::DOC];
        $result = $this->file();
        self::assertSame(403, $result['status']);
        self::assertSame('document_access_denied', $result['body']['detail']['code']);
        $this->assertNothingWritten();
    }

    public function testFilingNeedsLabWrite(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $result = $this->file(c: $this->controller(write: []));
        self::assertSame(403, $result['status']);
        self::assertSame('filing_not_permitted', $result['body']['detail']['code']);
        self::assertFalse($this->audit->events[0]['success']);
        $this->assertNothingWritten();
    }

    public function testFilingNeedsSign(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $result = $this->file(c: $this->controller(acl: self::READ_ACL));
        self::assertSame(403, $result['status']);
        self::assertSame('filing_not_permitted', $result['body']['detail']['code']);
        $this->assertNothingWritten();
    }

    public function testTheReadAuthorizerStillApplies(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        self::assertSame(403, $this->file(c: $this->controller(bases: []))['status']);
        self::assertSame(401, $this->controller()->fileForSession(self::session(null), self::DOC, 0, [])['status']);
        $this->assertNothingWritten();
    }

    public function testAHeldDocumentIsNotFiled(): void
    {
        $this->store->documents[self::DOC]['status'] = 'held_identity';
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $result = $this->file();
        self::assertSame(409, $result['status']);
        self::assertSame('document_not_extracted', $result['body']['detail']['code']);
        $this->assertNothingWritten();
    }

    public function testIntakeFormValuesAreNeverFiled(): void
    {
        $this->store->documents[self::DOC]['doc_type'] = 'intake_form';
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $result = $this->file();
        self::assertSame(409, $result['status']);
        self::assertSame('not_fileable', $result['body']['detail']['code']);
    }

    public function testARejectedValueCannotBeFiled(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0, ['status' => 'rejected']);
        $result = $this->file();
        self::assertSame(409, $result['status']);
        self::assertSame('value_rejected', $result['body']['detail']['code']);
        $this->assertNothingWritten();
    }

    public function testWithoutTheModuleTablesFilingIsDisabledWithAClearCode(): void
    {
        $result = $this->file(c: $this->controller(ready: false));
        self::assertSame(503, $result['status']);
        self::assertSame('copilot_tables_not_installed', $result['body']['detail']['code']);
        self::assertSame(0, $this->store->transactions);
    }

    public function testAMalformedBodyIs400(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        foreach ([null, ['filed_value' => 7.4], ['confirm_unverified' => 'yes'], ['filed_value' => str_repeat('9', 256)]] as $body) {
            $result = $this->file(0, $body);
            self::assertSame(400, $result['status'], json_encode($body));
            self::assertSame('invalid_request', $result['body']['detail']['code']);
        }
        self::assertSame(200, $this->file(0, [])['status'], 'both fields are optional');
    }

    // ------------------------------------------------------------------ //
    // Reject
    // ------------------------------------------------------------------ //

    public function testRejectMarksTheCandidateAndNeverFiles(): void
    {
        $cid = $this->store->addCandidate(self::DOC, self::PID, 0);

        $result = $this->controller()->rejectForSession(self::session(), self::DOC, 0);

        self::assertSame(200, $result['status']);
        self::assertSame('rejected', $result['body']['status']);
        self::assertSame('rejected', $this->candidate($cid)['status']);
        $this->assertNothingWritten();
        self::assertSame(FilingController::AUDIT_REJECTED, $this->audit->events[0]['event']);
        self::assertStringContainsString('outcome=rejected', $this->audit->events[0]['comment']);
        $this->assertNoPhi();

        $again = $this->controller()->rejectForSession(self::session(), self::DOC, 0);
        self::assertSame(200, $again['status']);
        self::assertStringContainsString('outcome=already_rejected', $this->audit->events[1]['comment']);
    }

    public function testAFiledValueCannotBeRejected(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $this->file();
        $result = $this->controller()->rejectForSession(self::session(), self::DOC, 0);
        self::assertSame(409, $result['status']);
        self::assertSame('value_already_filed', $result['body']['detail']['code']);
    }

    public function testRejectChecksOwnershipAndPermissions(): void
    {
        $this->store->addCandidate(self::OTHER_DOC, self::OTHER_PID, 0);
        self::assertSame(404, $this->controller()->rejectForSession(self::session(), self::OTHER_DOC, 0)['status']);
        $cid = $this->store->addCandidate(self::DOC, self::PID, 0);
        self::assertSame(403, $this->controller(write: [])->rejectForSession(self::session(), self::DOC, 0)['status']);
        self::assertSame('candidate', $this->candidate($cid)['status']);
    }

    // ------------------------------------------------------------------ //
    // Un-file (ADR-009 §7, 7b)
    // ------------------------------------------------------------------ //

    private function unfile(int $index = 0, int $doc = self::DOC, ?FilingController $c = null): array
    {
        return ($c ?? $this->controller())->unfileForSession(self::session(), $doc, $index);
    }

    public function testUnfileMarksTheChartResultEnteredInErrorAndKeepsHistory(): void
    {
        $cid = $this->store->addCandidate(self::DOC, self::PID, 0);
        $resultId = $this->file()['body']['procedure_result_id'];

        $result = $this->unfile();

        self::assertSame(200, $result['status'], json_encode($result['body']));
        self::assertSame('unfiled', $result['body']['status']);
        self::assertSame($resultId, $result['body']['procedure_result_id']);
        self::assertCount(1, $this->store->results, 'nothing is deleted');
        self::assertSame('entered-in-error', $this->store->results[$resultId]['result_status']);
        self::assertSame('7.1', $this->store->results[$resultId]['result']);
        $c = $this->candidate($cid);
        self::assertSame('unfiled', $c['status']);
        self::assertSame($resultId, $c['procedure_result_id'], 'the link to the withdrawn result stays');
        self::assertSame('7.1', $c['filed_value']);
        self::assertSame(self::USER, $c['filed_by']);

        $event = $this->audit->events[1];
        self::assertSame(FilingController::AUDIT_UNFILED, $event['event']);
        self::assertTrue($event['success']);
        self::assertStringContainsString('outcome=unfiled', $event['comment']);
        self::assertStringContainsString('procedure_result_id=' . $resultId, $event['comment']);
        $this->assertNoPhi();
    }

    public function testUnfileIsIdempotentAndReFilingIsRefused(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $this->file();
        $this->unfile();

        $again = $this->unfile();
        self::assertSame(200, $again['status']);
        self::assertTrue($again['body']['already_unfiled']);

        $refile = $this->file();
        self::assertSame(409, $refile['status']);
        self::assertSame('value_unfiled', $refile['body']['detail']['code']);
        self::assertCount(1, $this->store->results);

        $reject = $this->controller()->rejectForSession(self::session(), self::DOC, 0);
        self::assertSame(409, $reject['status']);
        self::assertSame('value_unfiled', $reject['body']['detail']['code']);
    }

    public function testOnlyAFiledValueCanBeUnfiled(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $this->store->addCandidate(self::DOC, self::PID, 1, ['status' => 'rejected']);
        foreach ([0, 1] as $index) {
            $r = $this->unfile($index);
            self::assertSame(409, $r['status']);
            self::assertSame('value_not_filed', $r['body']['detail']['code']);
        }
        self::assertSame(404, $this->unfile(7)['status']);
    }

    public function testUnfileIsOneTransaction(): void
    {
        $cid = $this->store->addCandidate(self::DOC, self::PID, 0);
        $resultId = $this->file()['body']['procedure_result_id'];
        $this->store->failAt = 'markUnfiled';

        self::assertSame(500, $this->unfile()['status']);

        self::assertSame('final', $this->store->results[$resultId]['result_status']);
        self::assertSame('filed', $this->candidate($cid)['status']);
    }

    public function testUnfileNeedsTheSamePermissionsAndOwnership(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $this->file();
        self::assertSame(403, $this->unfile(c: $this->controller(write: []))['status']);
        self::assertSame(403, $this->unfile(c: $this->controller(acl: self::READ_ACL))['status']);
        $this->store->addCandidate(self::OTHER_DOC, self::OTHER_PID, 0, ['status' => 'filed', 'procedure_result_id' => 5]);
        self::assertSame(404, $this->unfile(0, self::OTHER_DOC)['status']);
        $this->documents->denied = [self::DOC];
        self::assertSame(403, $this->unfile()['status']);
        self::assertSame('filed', array_values($this->store->candidates)[0]['status']);
    }

    /** A wrongly filed result must stay withdrawable after its document was deleted or moved away in OpenEMR. */
    public function testUnfileWorksWhenTheDocumentIsNoLongerFiledToThePatient(): void
    {
        $this->store->addCandidate(self::DOC, self::PID, 0);
        $this->file();
        unset($this->documents->docs[self::DOC]);

        self::assertSame(200, $this->unfile()['status']);
        self::assertSame(404, $this->file(0)['status'], 'filing still needs the document');
    }

    // ------------------------------------------------------------------ //
    // ADR-009 §4 guard
    // ------------------------------------------------------------------ //

    public function testTheSqlStoreNeverUsesServicesThatCommitTheTransaction(): void
    {
        $source = (string) file_get_contents(__DIR__ . '/../src/Filing/SqlFilingStore.php');
        self::assertStringContainsString('createUuid()', $source);
        $code = (string) preg_replace('#/\*.*?\*/#s', '', $source); // the doc comment may name what is forbidden
        foreach (['ProcedureService', 'FHIR', 'Fhir', 'createMissingUuids', 'commitTransaction', 'sqlCommitTrans', 'OpenEMR\\Services'] as $forbidden) {
            self::assertStringNotContainsString($forbidden, $code, $forbidden . ' commits or may commit the filing transaction');
        }
    }
}
