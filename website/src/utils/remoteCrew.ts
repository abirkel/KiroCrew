/**
 * Constants the Remote Crew surfaces share with the backend. They live here,
 * not in `api/client.ts`, because most test files replace that module with a
 * hand-written mock and a new export there breaks every one of them.
 */

/** Default warm-set cap when the gateway reports none. Mirrors
 *  WARM_SET_CAP_AUTO_CEILING in src/kiro_crew/instances/constants.py. */
export const DEFAULT_WARM_SET_CAP = 10

/** Provisioner id of the built-in EC2 launcher. Mirrors
 *  BUILTIN_PROVISIONER_ID in src/kiro_crew/platform/interfaces.py. */
export const BUILTIN_PROVISIONER_ID = 'aws_ec2'
