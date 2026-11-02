"""Prepare an explicit external-axis JOG input; never connect to PowerMill.

PowerMill Robot 2023's ROBOT/EXTERNALABS packet uses the static table-attach
frame. Project coordinates and physical cell coordinates are different inputs.
This is a pose diagnostic, not a replacement for toolpath playback, PP, time,
continuous collision validation, or the live Executor guards.
"""
from .core import HelperError
from .simulation import Kinematics, sub, scale, dot, cross, norm, unit, angle, vector, number


def prepare_external_axis_input(contract, source_pose, external_axes, table_attach, *, motion_type):
    """Return a native Cartesian packet with explicit, bounded table axes.

    ``table_attach`` contains origin/x/z in the same physical cell frame as the
    reviewed contract. Read it from the current MTD, not from a remembered cell.
    The caller must still check the fresh machine/tool contract, native outcome,
    actual FK/XYZ/Z/X, motion metadata, and collisions after execution.
    """
    kin = Kinematics(contract)
    if not isinstance(source_pose, (list, tuple)) or len(source_pose) != 6:
        raise HelperError('Source XYZ and programmed Z required')
    position = vector(source_pose[:3])
    programmed_z = unit(vector(source_pose[3:]))
    if not isinstance(external_axes, dict) or set(external_axes) != set(kin.table):
        raise HelperError('Exactly the table-chain axes must be supplied')
    values = {key: number(value) for key, value in external_axes.items()}
    for key, value in values.items():
        lo, hi = kin.axes[key]['limits']
        if not lo <= value <= hi:
            raise HelperError('External axis outside limits: ' + key)
        if key in kin.fixed and abs(value - kin.fixed[key]) > kin.tol['joint']:
            raise HelperError('Fixed external axis moved: ' + key)
    if not isinstance(table_attach, dict) or set(table_attach) != {'origin', 'x', 'z'}:
        raise HelperError('Explicit table-attach origin/X/Z required')
    origin = vector(table_attach['origin'])
    tx, tz = vector(table_attach['x']), vector(table_attach['z'])
    if abs(norm(tx)-1) > 1e-8 or abs(norm(tz)-1) > 1e-8 or abs(dot(tx,tz)) > 1e-8:
        raise HelperError('Table-attach basis must be orthonormal')
    ty = cross(tz, tx)
    tcp = kin.transform(kin.frame(position), kin.table, values)
    z = unit(kin.transform(kin.frame(programmed_z, True), kin.table, values, vector_only=True))
    projected_x = sub(kin.reference, scale(z, dot(kin.reference, z)))
    if norm(projected_x) < 1e-10:
        raise HelperError('Programmed Z prevents the required machine X')
    x = unit(projected_x)
    if angle(x, kin.reference) > kin.max_x:
        raise HelperError('Required tool X tolerance cannot be achieved')
    if not set(contract['region']['undo_axes']) <= set(values):
        raise HelperError('Region conversion requires unsupplied robot axes')
    relative = sub(kin.transform(tcp, contract['region']['undo_axes'], values, inverse=True), kin.region_origin)
    if any(v < lo or v > hi for v, lo, hi in zip(relative[:2], kin.region_min, kin.region_max)):
        raise HelperError('TCP outside working region')
    if kin.motion_regions:
        if not isinstance(motion_type, str) or motion_type not in kin.motion_regions:
            raise HelperError('Known source motion type required')
        _, lo, hi = kin.motion_regions[motion_type]
        if any(v < low or v > high for v, low, high in zip(relative[:2], lo, hi)):
            raise HelperError('TCP outside motion-specific region')
    basis = (tx, ty, tz)
    native_xyz = tuple(dot(axis, sub(tcp, origin)) for axis in basis)
    native_z = tuple(dot(axis, z) for axis in basis)
    native_x = tuple(dot(axis, x) for axis in basis)
    pose = native_xyz + native_z + native_x
    command = 'SIMULATE JOG ROBOT ' + ' '.join('%s %.12f' % item for item in zip('XYZIJKUVW', pose))
    command += ' EXTERNALABS ' + ' '.join('%s%.12f' % (key, values[key]) for key in kin.table)
    return {'command': command, 'native_pose': pose, 'world_TCP': tcp,
            'world_Z': z, 'world_X': x, 'region_XY': relative[:2],
            'external_axes': values, 'motion_type': motion_type,
            'native_validation_required': True}
