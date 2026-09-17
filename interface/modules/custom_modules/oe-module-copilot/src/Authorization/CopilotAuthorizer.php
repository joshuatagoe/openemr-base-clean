<?php

/**
 * Server-side authorization for a copilot context read.
 *
 * Fail-closed, in this order: authenticated user -> a patient selected in the
 * session -> the phpGACL sections the bundle and sections draw on -> a care
 * relationship between user and patient (or, when the module global
 * `copilot_admin_relationship_override` is on, admin/super with an audited
 * "admin_override" basis). The patient id is never accepted from the client.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Authorization;

final class CopilotAuthorizer
{
    public const CODE_OK = 'ok';
    public const CODE_NOT_AUTHENTICATED = 'not_authenticated';
    public const CODE_NO_PATIENT_SELECTED = 'no_patient_selected';
    public const CODE_ACL_DENIED = 'acl_denied';
    public const CODE_NO_CARE_RELATIONSHIP = 'no_care_relationship';

    public const BASIS_ADMIN_OVERRIDE = 'admin_override';

    /** ACL sections the bundle's sources require (ARCHITECTURE.md section 6). */
    public const REQUIRED_ACLS = [
        ['patients', 'demo'],
        ['encounters', 'auth_a'],
        ['encounters', 'notes'],
        ['patients', 'med'],
        ['patients', 'lab'],
        ['patients', 'appt'],
    ];

    public function __construct(
        private readonly AclCheckerInterface $acl,
        private readonly RelationshipRepositoryInterface $relationships,
        private readonly bool $adminOverrideEnabled = false,
    ) {
    }

    /**
     * @return array{allowed:bool, code:string, basis:?string}
     */
    public function authorize(?int $userId, ?string $username, ?int $pid): array
    {
        if ($userId === null || $userId <= 0 || $username === null || $username === '') {
            return ['allowed' => false, 'code' => self::CODE_NOT_AUTHENTICATED, 'basis' => null];
        }
        if ($pid === null || $pid <= 0) {
            return ['allowed' => false, 'code' => self::CODE_NO_PATIENT_SELECTED, 'basis' => null];
        }
        foreach (self::REQUIRED_ACLS as [$section, $value]) {
            if (!$this->acl->check($section, $value, $username)) {
                return ['allowed' => false, 'code' => self::CODE_ACL_DENIED, 'basis' => null];
            }
        }

        $basis = $this->relationships->findBasis($userId, $pid);
        if ($basis !== null) {
            return ['allowed' => true, 'code' => self::CODE_OK, 'basis' => $basis];
        }
        if ($this->adminOverrideEnabled && $this->acl->check('admin', 'super', $username)) {
            return ['allowed' => true, 'code' => self::CODE_OK, 'basis' => self::BASIS_ADMIN_OVERRIDE];
        }
        return ['allowed' => false, 'code' => self::CODE_NO_CARE_RELATIONSHIP, 'basis' => null];
    }
}
