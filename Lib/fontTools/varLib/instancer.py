""" Partially instantiate a variable font.

The module exports an `instantiateVariableFont` function and CLI that allow to
create full instances (i.e. static fonts) from variable fonts, as well as "partial"
variable fonts that only contain a subset of the original variation space.

For example, if you wish to pin the width axis to a given location while also
restricting the weight axis to 400..700 range, you can do:

$ fonttools varLib.instancer ./NotoSans-VF.ttf wdth=85 wght=400:700

See `fonttools varLib.instancer --help` for more info on the CLI options.

The module's entry point is the `instantiateVariableFont` function, which takes
a TTFont object and a dict specifying either axis coodinates or (min, max) ranges,
and returns a new TTFont representing either a partial VF, or full instance if all
the VF axes were given an explicit coordinate.

E.g. here's how to pin the wght axis at a given location in a wght+wdth variable
font, keeping only the deltas associated with the wdth axis:

| >>> from fontTools import ttLib
| >>> from fontTools.varLib import instancer
| >>> varfont = ttLib.TTFont("path/to/MyVariableFont.ttf")
| >>> [a.axisTag for a in partial["fvar"].axes]  # the varfont's current axes
| ['wght', 'wdth']
| >>> partial = instancer.instantiateVariableFont(varfont, {"wght": 300})
| >>> [a.axisTag for a in partial["fvar"].axes]  # axes left after pinning 'wght'
| ['wdth']

If the input location specifies all the axes, the resulting instance is no longer
'variable' (same as using fontools varLib.mutator):

| >>> instance = instancer.instantiateVariableFont(
| ...     varfont, {"wght": 700, "wdth": 67.5}
| ... )
| >>> "fvar" not in instance
| True

If one just want to drop an axis at the default location, without knowing in
advance what the default value for that axis is, one can pass a `None` value:

| >>> instance = instancer.instantiateVariableFont(varfont, {"wght": None})
| >>> len(varfont["fvar"].axes)
| 1

From the console script, this is equivalent to passing `wght=drop` as input.

This module is similar to fontTools.varLib.mutator, which it's intended to supersede.
Note that, unlike varLib.mutator, when an axis is not mentioned in the input
location, the varLib.instancer will keep the axis and the corresponding deltas,
whereas mutator implicitly drops the axis at its default coordinate.

The module currently supports only the first three "levels" of partial instancing,
with the rest planned to be implemented in the future, namely:
L1) dropping one or more axes while leaving the default tables unmodified;
L2) dropping one or more axes while pinning them at non-default locations;
L3) restricting the range of variation of one or more axes, by setting either
    a new minimum or maximum, potentially -- though not necessarily -- dropping
    entire regions of variations that fall completely outside this new range.
L4) moving the default location of an axis.

Currently only TrueType-flavored variable fonts (i.e. containing 'glyf' table)
are supported, but support for CFF2 variable fonts will be added soon.

The discussion and implementation of these features are tracked at
https://github.com/fonttools/fonttools/issues/1537
"""
from fontTools.misc.fixedTools import (
    floatToFixedToFloat,
    strToFixedToFloat,
    otRound,
    MAX_F2DOT14,
)
from fontTools.varLib.models import supportScalar, normalizeValue, piecewiseLinearMap
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables import _g_l_y_f
from fontTools import varLib

# we import the `subset` module because we use the `prune_lookups` method on the GSUB
# table class, and that method is only defined dynamically upon importing `subset`
from fontTools import subset  # noqa: F401
from fontTools.varLib import builder
from fontTools.varLib.mvar import MVAR_ENTRIES
from fontTools.varLib.merger import MutatorMerger
from contextlib import contextmanager
import collections
from copy import deepcopy
import logging
from itertools import islice
import os
import re


log = logging.getLogger("fontTools.varLib.instancer")


class AxisRange(collections.namedtuple("AxisRange", "minimum maximum")):
    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls, *args, **kwargs)
        if self.minimum > self.maximum:
            raise ValueError(
                f"Range minimum ({self.minimum:g}) must be <= maximum ({self.maximum:g})"
            )
        return self

    def __repr__(self):
        return f"{type(self).__name__}({self.minimum:g}, {self.maximum:g})"


class NormalizedAxisRange(AxisRange):
    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls, *args, **kwargs)
        if self.minimum < -1.0 or self.maximum > 1.0:
            raise ValueError("Axis range values must be normalized to -1..+1 range")
        if self.minimum > 0:
            raise ValueError(f"Expected axis range minimum <= 0; got {self.minimum}")
        if self.maximum < 0:
            raise ValueError(f"Expected axis range maximum >= 0; got {self.maximum}")
        return self


def instantiateTupleVariationStore(
    variations, axisLimits, origCoords=None, endPts=None
):
    """Instantiate TupleVariation list at the given location, or limit axes' min/max.

    The 'variations' list of TupleVariation objects is modified in-place.
    The 'axisLimits' (dict) maps axis tags (str) to either a single coordinate along the
    axis (float), or to minimum/maximum coordinates (NormalizedAxisRange).

    A 'full' instance (i.e. static font) is produced when all the axes are pinned to
    single coordinates; a 'partial' instance (i.e. a less variable font) is produced
    when some of the axes are omitted, or restricted with a new range.

    Tuples that do not participate are kept as they are. Those that have 0 influence
    at the given location are removed from the variation store.
    Those that are fully instantiated (i.e. all their axes are being pinned) are also
    removed from the variation store, their scaled deltas accummulated and returned, so
    that they can be added by the caller to the default instance's coordinates.
    Tuples that are only partially instantiated (i.e. not all the axes that they
    participate in are being pinned) are kept in the store, and their deltas multiplied
    by the scalar support of the axes to be pinned at the desired location.

    Args:
        variations: List[TupleVariation] from either 'gvar' or 'cvar'.
        axisLimits: Dict[str, Union[float, NormalizedAxisRange]]: axes' coordinates for
            the full or partial instance, or ranges for restricting an axis' min/max.
        origCoords: GlyphCoordinates: default instance's coordinates for computing 'gvar'
            inferred points (cf. table__g_l_y_f.getCoordinatesAndControls).
        endPts: List[int]: indices of contour end points, for inferring 'gvar' deltas.

    Returns:
        List[float]: the overall delta adjustment after applicable deltas were summed.
    """
    pinnedLocation, axisRanges = splitAxisLocationAndRanges(
        axisLimits, rangeType=NormalizedAxisRange
    )

    newVariations = variations

    if pinnedLocation:
        newVariations = pinTupleVariationAxes(variations, pinnedLocation)

    if axisRanges:
        newVariations = limitTupleVariationAxisRanges(newVariations, axisRanges)

    mergedVariations = collections.OrderedDict()
    for var in newVariations:
        # compute inferred deltas only for gvar ('origCoords' is None for cvar)
        if origCoords is not None:
            var.calcInferredDeltas(origCoords, endPts)

        # merge TupleVariations with overlapping "tents"
        axes = frozenset(var.axes.items())
        if axes in mergedVariations:
            mergedVariations[axes] += var
        else:
            mergedVariations[axes] = var

    # drop TupleVariation if all axes have been pinned (var.axes.items() is empty);
    # its deltas will be added to the default instance's coordinates
    defaultVar = mergedVariations.pop(frozenset(), None)

    for var in mergedVariations.values():
        var.roundDeltas()
    variations[:] = list(mergedVariations.values())

    return defaultVar.coordinates if defaultVar is not None else []


def pinTupleVariationAxes(variations, location):
    newVariations = []
    for var in variations:
        # Compute the scalar support of the axes to be pinned at the desired location,
        # excluding any axes that we are not pinning.
        # If a TupleVariation doesn't mention an axis, it implies that the axis peak
        # is 0 (i.e. the axis does not participate).
        support = {axis: var.axes.pop(axis, (-1, 0, +1)) for axis in location}
        scalar = supportScalar(location, support)
        if scalar == 0.0:
            # no influence, drop the TupleVariation
            continue

        var.scaleDeltas(scalar)
        newVariations.append(var)
    return newVariations


def limitTupleVariationAxisRanges(variations, axisRanges):
    for axisTag, axisRange in sorted(axisRanges.items()):
        newVariations = []
        for var in variations:
            newVariations.extend(limitTupleVariationAxisRange(var, axisTag, axisRange))
        variations = newVariations
    return variations


def _negate(*values):
    yield from (-1 * v for v in values)


def limitTupleVariationAxisRange(var, axisTag, axisRange):
    if not isinstance(axisRange, NormalizedAxisRange):
        axisRange = NormalizedAxisRange(*axisRange)

    # skip when current axis is missing (i.e. doesn't participate), or when the
    # 'tent' isn't fully on either the negative or positive side
    lower, peak, upper = var.axes.get(axisTag, (-1, 0, 1))
    if peak == 0 or lower > peak or peak > upper or (lower < 0 and upper > 0):
        return [var]

    negative = lower < 0
    if negative:
        if axisRange.minimum == -1.0:
            return [var]
        elif axisRange.minimum == 0.0:
            return []
    else:
        if axisRange.maximum == 1.0:
            return [var]
        elif axisRange.maximum == 0.0:
            return []

    limit = axisRange.minimum if negative else axisRange.maximum

    # Rebase axis bounds onto the new limit, which then becomes the new -1.0 or +1.0.
    # The results are always positive, because both dividend and divisor are either
    # all positive or all negative.
    newLower = lower / limit
    newPeak = peak / limit
    newUpper = upper / limit
    # for negative TupleVariation, swap lower and upper to simplify procedure
    if negative:
        newLower, newUpper = newUpper, newLower

    # special case when innermost bound == peak == limit
    if newLower == newPeak == 1.0:
        var.axes[axisTag] = (-1.0, -1.0, -1.0) if negative else (1.0, 1.0, 1.0)
        return [var]

    # case 1: the whole deltaset falls outside the new limit; we can drop it
    elif newLower >= 1.0:
        return []

    # case 2: only the peak and outermost bound fall outside the new limit;
    # we keep the deltaset, update peak and outermost bound and and scale deltas
    # by the scalar value for the restricted axis at the new limit.
    elif newPeak >= 1.0:
        scalar = supportScalar({axisTag: limit}, {axisTag: (lower, peak, upper)})
        var.scaleDeltas(scalar)
        newPeak = 1.0
        newUpper = 1.0
        if negative:
            newLower, newPeak, newUpper = _negate(newUpper, newPeak, newLower)
        var.axes[axisTag] = (newLower, newPeak, newUpper)
        return [var]

    # case 3: peak falls inside but outermost limit still fits within F2Dot14 bounds;
    # we keep deltas as is and only scale the axes bounds. Deltas beyond -1.0
    # or +1.0 will never be applied as implementations must clap to that range.
    elif newUpper <= 2.0:
        if negative:
            newLower, newPeak, newUpper = _negate(newUpper, newPeak, newLower)
        elif MAX_F2DOT14 < newUpper <= 2.0:
            # we clamp +2.0 to the max F2Dot14 (~1.99994) for convenience
            newUpper = MAX_F2DOT14
        var.axes[axisTag] = (newLower, newPeak, newUpper)
        return [var]

    # case 4: new limit doesn't fit, we need to chop the tent into two triangles,
    # with an additional tent with scaled-down deltas that peaks as the original
    # one tapers down. NOTE: This increases the file size!
    else:
        newVar = TupleVariation(var.axes, var.coordinates)
        if negative:
            var.axes[axisTag] = (-2.0, -1 * newPeak, -1 * newLower)
            newVar.axes[axisTag] = (-1.0, -1.0, -1 * newPeak)
        else:
            var.axes[axisTag] = (newLower, newPeak, MAX_F2DOT14)
            newVar.axes[axisTag] = (newPeak, 1.0, 1.0)
        # TODO: document optimization
        scalar1 = supportScalar({axisTag: limit}, {axisTag: (lower, peak, upper)})
        scalar2 = 1 / (2 - newPeak)
        newVar.scaleDeltas(scalar1 - scalar2)

        return [var, newVar]


def instantiateGvarGlyph(varfont, glyphname, location, optimize=True):
    glyf = varfont["glyf"]
    coordinates, ctrl = glyf.getCoordinatesAndControls(glyphname, varfont)
    endPts = ctrl.endPts

    gvar = varfont["gvar"]
    # when exporting to TTX, a glyph with no variations is omitted; thus when loading
    # a TTFont from TTX, a glyph that's present in glyf table may be missing from gvar.
    tupleVarStore = gvar.variations.get(glyphname)

    if tupleVarStore:
        defaultDeltas = instantiateTupleVariationStore(
            tupleVarStore, location, coordinates, endPts
        )

        if defaultDeltas:
            coordinates += _g_l_y_f.GlyphCoordinates(defaultDeltas)

    # setCoordinates also sets the hmtx/vmtx advance widths and sidebearings from
    # the four phantom points and glyph bounding boxes.
    # We call it unconditionally even if a glyph has no variations or no deltas are
    # applied at this location, in case the glyph's xMin and in turn its sidebearing
    # have changed. E.g. a composite glyph has no deltas for the component's (x, y)
    # offset nor for the 4 phantom points (e.g. it's monospaced). Thus its entry in
    # gvar table is empty; however, the composite's base glyph may have deltas
    # applied, hence the composite's bbox and left/top sidebearings may need updating
    # in the instanced font.
    glyf.setCoordinates(glyphname, coordinates, varfont)

    if not tupleVarStore:
        if glyphname in gvar.variations:
            del gvar.variations[glyphname]
        return

    if optimize:
        isComposite = glyf[glyphname].isComposite()
        for var in tupleVarStore:
            var.optimize(coordinates, endPts, isComposite)


def instantiateGvar(varfont, location, optimize=True):
    log.info("Instantiating glyf/gvar tables")

    gvar = varfont["gvar"]
    glyf = varfont["glyf"]
    # Get list of glyph names sorted by component depth.
    # If a composite glyph is processed before its base glyph, the bounds may
    # be calculated incorrectly because deltas haven't been applied to the
    # base glyph yet.
    glyphnames = sorted(
        glyf.glyphOrder,
        key=lambda name: (
            glyf[name].getCompositeMaxpValues(glyf).maxComponentDepth
            if glyf[name].isComposite()
            else 0,
            name,
        ),
    )
    for glyphname in glyphnames:
        instantiateGvarGlyph(varfont, glyphname, location, optimize=optimize)

    if not gvar.variations:
        del varfont["gvar"]


def setCvarDeltas(cvt, deltas):
    for i, delta in enumerate(deltas):
        if delta:
            cvt[i] += otRound(delta)


def instantiateCvar(varfont, location):
    log.info("Instantiating cvt/cvar tables")

    cvar = varfont["cvar"]

    defaultDeltas = instantiateTupleVariationStore(cvar.variations, location)

    if defaultDeltas:
        setCvarDeltas(varfont["cvt "], defaultDeltas)

    if not cvar.variations:
        del varfont["cvar"]


def setMvarDeltas(varfont, deltas):
    mvar = varfont["MVAR"].table
    records = mvar.ValueRecord
    for rec in records:
        mvarTag = rec.ValueTag
        if mvarTag not in MVAR_ENTRIES:
            continue
        tableTag, itemName = MVAR_ENTRIES[mvarTag]
        delta = deltas[rec.VarIdx]
        if delta != 0:
            setattr(
                varfont[tableTag],
                itemName,
                getattr(varfont[tableTag], itemName) + otRound(delta),
            )


def instantiateMVAR(varfont, location):
    log.info("Instantiating MVAR table")

    mvar = varfont["MVAR"].table
    fvarAxes = varfont["fvar"].axes
    varStore = mvar.VarStore
    defaultDeltas = instantiateItemVariationStore(varStore, fvarAxes, location)
    setMvarDeltas(varfont, defaultDeltas)

    if varStore.VarRegionList.Region:
        varIndexMapping = varStore.optimize()
        for rec in mvar.ValueRecord:
            rec.VarIdx = varIndexMapping[rec.VarIdx]
    else:
        del varfont["MVAR"]


def _remapVarIdxMap(table, attrName, varIndexMapping, glyphOrder):
    oldMapping = getattr(table, attrName).mapping
    newMapping = [varIndexMapping[oldMapping[glyphName]] for glyphName in glyphOrder]
    setattr(table, attrName, builder.buildVarIdxMap(newMapping, glyphOrder))


# TODO(anthrotype) Add support for HVAR/VVAR in CFF2
def _instantiateVHVAR(varfont, axisLimits, tableFields):
    tableTag = tableFields.tableTag
    fvarAxes = varfont["fvar"].axes
    # Deltas from gvar table have already been applied to the hmtx/vmtx. For full
    # instances (i.e. all axes pinned), we can simply drop HVAR/VVAR and return
    if set(
        axisTag for axisTag, value in axisLimits.items() if not isinstance(value, tuple)
    ).issuperset(axis.axisTag for axis in fvarAxes):
        log.info("Dropping %s table", tableTag)
        del varfont[tableTag]
        return

    log.info("Instantiating %s table", tableTag)
    vhvar = varfont[tableTag].table
    varStore = vhvar.VarStore
    # since deltas were already applied, the return value here is ignored
    instantiateItemVariationStore(varStore, fvarAxes, axisLimits)

    if varStore.VarRegionList.Region:
        # Only re-optimize VarStore if the HVAR/VVAR already uses indirect AdvWidthMap
        # or AdvHeightMap. If a direct, implicit glyphID->VariationIndex mapping is
        # used for advances, skip re-optimizing and maintain original VariationIndex.
        if getattr(vhvar, tableFields.advMapping):
            varIndexMapping = varStore.optimize()
            glyphOrder = varfont.getGlyphOrder()
            _remapVarIdxMap(vhvar, tableFields.advMapping, varIndexMapping, glyphOrder)
            if getattr(vhvar, tableFields.sb1):  # left or top sidebearings
                _remapVarIdxMap(vhvar, tableFields.sb1, varIndexMapping, glyphOrder)
            if getattr(vhvar, tableFields.sb2):  # right or bottom sidebearings
                _remapVarIdxMap(vhvar, tableFields.sb2, varIndexMapping, glyphOrder)
            if tableTag == "VVAR" and getattr(vhvar, tableFields.vOrigMapping):
                _remapVarIdxMap(
                    vhvar, tableFields.vOrigMapping, varIndexMapping, glyphOrder
                )


def instantiateHVAR(varfont, location):
    return _instantiateVHVAR(varfont, location, varLib.HVAR_FIELDS)


def instantiateVVAR(varfont, location):
    return _instantiateVHVAR(varfont, location, varLib.VVAR_FIELDS)


class _TupleVarStoreAdapter(object):
    def __init__(self, regions, axisOrder, tupleVarData, itemCounts):
        self.regions = regions
        self.axisOrder = axisOrder
        self.tupleVarData = tupleVarData
        self.itemCounts = itemCounts

    @classmethod
    def fromItemVarStore(cls, itemVarStore, fvarAxes):
        axisOrder = [axis.axisTag for axis in fvarAxes]
        regions = [
            region.get_support(fvarAxes) for region in itemVarStore.VarRegionList.Region
        ]
        tupleVarData = []
        itemCounts = []
        for varData in itemVarStore.VarData:
            variations = []
            varDataRegions = (regions[i] for i in varData.VarRegionIndex)
            for axes, coordinates in zip(varDataRegions, zip(*varData.Item)):
                variations.append(TupleVariation(axes, list(coordinates)))
            tupleVarData.append(variations)
            itemCounts.append(varData.ItemCount)
        return cls(regions, axisOrder, tupleVarData, itemCounts)

    def rebuildRegions(self):
        # Collect the set of all unique region axes from the current TupleVariations.
        # We use an OrderedDict to de-duplicate regions while keeping the order.
        uniqueRegions = collections.OrderedDict.fromkeys(
            (
                frozenset(var.axes.items())
                for variations in self.tupleVarData
                for var in variations
            )
        )
        # Maintain the original order for the regions that pre-existed, appending
        # the new regions at the end of the region list.
        newRegions = []
        for region in self.regions:
            regionAxes = frozenset(region.items())
            if regionAxes in uniqueRegions:
                newRegions.append(region)
                del uniqueRegions[regionAxes]
        if uniqueRegions:
            newRegions.extend(dict(region) for region in uniqueRegions)
        self.regions = newRegions

    def instantiate(self, axisLimits):
        defaultDeltaArray = []
        for variations, itemCount in zip(self.tupleVarData, self.itemCounts):
            defaultDeltas = instantiateTupleVariationStore(variations, axisLimits)
            if not defaultDeltas:
                defaultDeltas = [0] * itemCount
            defaultDeltaArray.append(defaultDeltas)

        # rebuild regions whose axes were dropped or limited
        self.rebuildRegions()

        pinnedAxes = {
            axisTag
            for axisTag, value in axisLimits.items()
            if not isinstance(value, tuple)
        }
        self.axisOrder = [
            axisTag for axisTag in self.axisOrder if axisTag not in pinnedAxes
        ]

        return defaultDeltaArray

    def asItemVarStore(self):
        regionOrder = [frozenset(axes.items()) for axes in self.regions]
        varDatas = []
        for variations, itemCount in zip(self.tupleVarData, self.itemCounts):
            if variations:
                assert len(variations[0].coordinates) == itemCount
                varRegionIndices = [
                    regionOrder.index(frozenset(var.axes.items())) for var in variations
                ]
                varDataItems = list(zip(*(var.coordinates for var in variations)))
                varDatas.append(
                    builder.buildVarData(varRegionIndices, varDataItems, optimize=False)
                )
            else:
                varDatas.append(
                    builder.buildVarData([], [[] for _ in range(itemCount)])
                )
        regionList = builder.buildVarRegionList(self.regions, self.axisOrder)
        itemVarStore = builder.buildVarStore(regionList, varDatas)
        # remove unused regions from VarRegionList
        itemVarStore.prune_regions()
        return itemVarStore


def instantiateItemVariationStore(itemVarStore, fvarAxes, axisLimits):
    """ Compute deltas at partial location, and update varStore in-place.

    Remove regions in which all axes were instanced, and scale the deltas of
    the remaining regions where only some of the axes were instanced.

    The number of VarData subtables, and the number of items within each, are
    not modified, in order to keep the existing VariationIndex valid.
    One may call VarStore.optimize() method after this to further optimize those.

    Args:
        varStore: An otTables.VarStore object (Item Variation Store)
        fvarAxes: list of fvar's Axis objects
        location: Dict[str, float] mapping axis tags to normalized axis coordinates.
            May not specify coordinates for all the fvar axes.

    Returns:
        defaultDeltas: to be added to the default instance, of type dict of floats
            keyed by VariationIndex compound values: i.e. (outer << 16) + inner.
    """
    tupleVarStore = _TupleVarStoreAdapter.fromItemVarStore(itemVarStore, fvarAxes)
    defaultDeltaArray = tupleVarStore.instantiate(axisLimits)
    newItemVarStore = tupleVarStore.asItemVarStore()

    itemVarStore.VarRegionList = newItemVarStore.VarRegionList
    assert itemVarStore.VarDataCount == newItemVarStore.VarDataCount
    itemVarStore.VarData = newItemVarStore.VarData

    defaultDeltas = {
        ((major << 16) + minor): delta
        for major, deltas in enumerate(defaultDeltaArray)
        for minor, delta in enumerate(deltas)
    }
    return defaultDeltas


def instantiateOTL(varfont, location):
    # TODO(anthrotype) Support partial instancing of JSTF and BASE tables

    if (
        "GDEF" not in varfont
        or varfont["GDEF"].table.Version < 0x00010003
        or not varfont["GDEF"].table.VarStore
    ):
        return

    if "GPOS" in varfont:
        msg = "Instantiating GDEF and GPOS tables"
    else:
        msg = "Instantiating GDEF table"
    log.info(msg)

    gdef = varfont["GDEF"].table
    varStore = gdef.VarStore
    fvarAxes = varfont["fvar"].axes

    defaultDeltas = instantiateItemVariationStore(varStore, fvarAxes, location)

    # When VF are built, big lookups may overflow and be broken into multiple
    # subtables. MutatorMerger (which inherits from AligningMerger) reattaches
    # them upon instancing, in case they can now fit a single subtable (if not,
    # they will be split again upon compilation).
    # This 'merger' also works as a 'visitor' that traverses the OTL tables and
    # calls specific methods when instances of a given type are found.
    # Specifically, it adds default deltas to GPOS Anchors/ValueRecords and GDEF
    # LigatureCarets, and optionally deletes all VariationIndex tables if the
    # VarStore is fully instanced.
    merger = MutatorMerger(
        varfont, defaultDeltas, deleteVariations=(not varStore.VarRegionList.Region)
    )
    merger.mergeTables(varfont, [varfont], ["GDEF", "GPOS"])

    if varStore.VarRegionList.Region:
        varIndexMapping = varStore.optimize()
        gdef.remap_device_varidxes(varIndexMapping)
        if "GPOS" in varfont:
            varfont["GPOS"].table.remap_device_varidxes(varIndexMapping)
    else:
        # Downgrade GDEF.
        del gdef.VarStore
        gdef.Version = 0x00010002
        if gdef.MarkGlyphSetsDef is None:
            del gdef.MarkGlyphSetsDef
            gdef.Version = 0x00010000

        if not (
            gdef.LigCaretList
            or gdef.MarkAttachClassDef
            or gdef.GlyphClassDef
            or gdef.AttachList
            or (gdef.Version >= 0x00010002 and gdef.MarkGlyphSetsDef)
        ):
            del varfont["GDEF"]


def instantiateFeatureVariations(varfont, axisLimits):
    for tableTag in ("GPOS", "GSUB"):
        if tableTag not in varfont or not hasattr(
            varfont[tableTag].table, "FeatureVariations"
        ):
            continue
        log.info("Instantiating FeatureVariations of %s table", tableTag)
        _instantiateFeatureVariations(
            varfont[tableTag].table, varfont["fvar"].axes, axisLimits
        )
        # remove unreferenced lookups
        varfont[tableTag].prune_lookups()


def _featureVariationRecordIsUnique(rec, seen):
    conditionSet = []
    for cond in rec.ConditionSet.ConditionTable:
        if cond.Format != 1:
            # can't tell whether this is duplicate, assume is unique
            return True
        conditionSet.append(
            (cond.AxisIndex, cond.FilterRangeMinValue, cond.FilterRangeMaxValue)
        )
    # besides the set of conditions, we also include the FeatureTableSubstitution
    # version to identify unique FeatureVariationRecords, even though only one
    # version is currently defined. It's theoretically possible that multiple
    # records with same conditions but different substitution table version be
    # present in the same font for backward compatibility.
    recordKey = frozenset([rec.FeatureTableSubstitution.Version] + conditionSet)
    if recordKey in seen:
        return False
    else:
        seen.add(recordKey)  # side effect
        return True


def _limitFeatureVariationConditionRange(condition, axisRange):
    minValue = condition.FilterRangeMinValue
    maxValue = condition.FilterRangeMaxValue

    if (
        minValue > maxValue
        or minValue > axisRange.maximum
        or maxValue < axisRange.minimum
    ):
        # condition invalid or out of range
        return

    values = [minValue, maxValue]
    for i, value in enumerate(values):
        if value < 0:
            if axisRange.minimum == 0:
                newValue = 0
            else:
                newValue = value / abs(axisRange.minimum)
                if newValue <= -1.0:
                    newValue = -1.0
        elif value > 0:
            if axisRange.maximum == 0:
                newValue = 0
            else:
                newValue = value / axisRange.maximum
                if newValue >= 1.0:
                    newValue = 1.0
        else:
            newValue = 0
        values[i] = newValue

    # TODO(anthrotype): Is (0,0) condition supposed to be applied ever? Ask Behdad
    # if not any(values):
    #     return

    return AxisRange(*values)


def _instantiateFeatureVariationRecord(
    record, recIdx, location, fvarAxes, axisIndexMap
):
    applies = True
    newConditions = []
    for i, condition in enumerate(record.ConditionSet.ConditionTable):
        if condition.Format == 1:
            axisIdx = condition.AxisIndex
            axisTag = fvarAxes[axisIdx].axisTag
            if axisTag in location:
                minValue = condition.FilterRangeMinValue
                maxValue = condition.FilterRangeMaxValue
                v = location[axisTag]
                if not (minValue <= v <= maxValue):
                    # condition not met so remove entire record
                    applies = False
                    newConditions = None
                    break
            else:
                # axis not pinned, keep condition with remapped axis index
                applies = False
                condition.AxisIndex = axisIndexMap[axisTag]
                newConditions.append(condition)
        else:
            log.warning(
                "Condition table {0} of FeatureVariationRecord {1} has "
                "unsupported format ({2}); ignored".format(i, recIdx, condition.Format)
            )
            applies = False
            newConditions.append(condition)

    if newConditions:
        record.ConditionSet.ConditionTable = newConditions
        shouldKeep = True
    else:
        shouldKeep = False

    return applies, shouldKeep


def _limitFeatureVariationRecord(record, axisRanges, fvarAxes):
    newConditions = []
    for i, condition in enumerate(record.ConditionSet.ConditionTable):
        if condition.Format == 1:
            axisIdx = condition.AxisIndex
            axisTag = fvarAxes[axisIdx].axisTag
            if axisTag in axisRanges:
                axisRange = axisRanges[axisTag]
                newRange = _limitFeatureVariationConditionRange(condition, axisRange)
                if newRange:
                    # keep condition with updated limits and remapped axis index
                    condition.FilterRangeMinValue = newRange.minimum
                    condition.FilterRangeMaxValue = newRange.maximum
                    newConditions.append(condition)
                else:
                    # condition out of range, remove entire record
                    newConditions = None
                    break
            else:
                newConditions.append(condition)
        else:
            newConditions.append(condition)

    if newConditions:
        record.ConditionSet.ConditionTable = newConditions
        shouldKeep = True
    else:
        shouldKeep = False

    return shouldKeep


def _instantiateFeatureVariations(table, fvarAxes, axisLimits):
    location, axisRanges = splitAxisLocationAndRanges(
        axisLimits, rangeType=NormalizedAxisRange
    )
    pinnedAxes = set(location.keys())
    axisOrder = [axis.axisTag for axis in fvarAxes if axis.axisTag not in pinnedAxes]
    axisIndexMap = {axisTag: axisOrder.index(axisTag) for axisTag in axisOrder}

    featureVariationApplied = False
    uniqueRecords = set()
    newRecords = []

    for i, record in enumerate(table.FeatureVariations.FeatureVariationRecord):
        applies, shouldKeep = _instantiateFeatureVariationRecord(
            record, i, location, fvarAxes, axisIndexMap
        )
        if shouldKeep:
            shouldKeep = _limitFeatureVariationRecord(record, axisRanges, fvarAxes)

        if shouldKeep and _featureVariationRecordIsUnique(record, uniqueRecords):
            newRecords.append(record)

        if applies and not featureVariationApplied:
            assert record.FeatureTableSubstitution.Version == 0x00010000
            for rec in record.FeatureTableSubstitution.SubstitutionRecord:
                table.FeatureList.FeatureRecord[rec.FeatureIndex].Feature = rec.Feature
            # Set variations only once
            featureVariationApplied = True

    if newRecords:
        table.FeatureVariations.FeatureVariationRecord = newRecords
        table.FeatureVariations.FeatureVariationCount = len(newRecords)
    else:
        del table.FeatureVariations


def _isValidAvarSegmentMap(axisTag, segmentMap):
    if not segmentMap:
        return True
    if not {(-1.0, -1.0), (0, 0), (1.0, 1.0)}.issubset(segmentMap.items()):
        log.warning(
            f"Invalid avar SegmentMap record for axis '{axisTag}': does not "
            "include all required value maps {-1.0: -1.0, 0: 0, 1.0: 1.0}"
        )
        return False
    previousValue = None
    for fromCoord, toCoord in sorted(segmentMap.items()):
        if previousValue is not None and previousValue > toCoord:
            log.warning(
                f"Invalid avar AxisValueMap({fromCoord}, {toCoord}) record "
                f"for axis '{axisTag}': the toCoordinate value must be >= to "
                f"the toCoordinate value of the preceding record ({previousValue})."
            )
            return False
        previousValue = toCoord
    return True


def instantiateAvar(varfont, axisLimits):
    # 'axisLimits' dict must contain user-space (non-normalized) coordinates.

    location, axisRanges = splitAxisLocationAndRanges(axisLimits)

    segments = varfont["avar"].segments

    # drop table if we instantiate all the axes
    pinnedAxes = set(location.keys())
    if pinnedAxes.issuperset(segments):
        log.info("Dropping avar table")
        del varfont["avar"]
        return

    log.info("Instantiating avar table")
    for axis in pinnedAxes:
        if axis in segments:
            del segments[axis]

    # First compute the default normalization for axisRanges coordinates: i.e.
    # min = -1.0, default = 0, max = +1.0, and in between values interpolated linearly,
    # without using the avar table's mappings.
    # Then, for each axis' SegmentMap, if we are restricting its, compute the new
    # mappings by dividing the key/value pairs by the desired new min/max values,
    # dropping any mappings that fall outside the restricted range.
    normalizedRanges = normalizeAxisLimits(varfont, axisRanges, usingAvar=False)
    newSegments = {}
    for axisTag, mapping in segments.items():
        if not _isValidAvarSegmentMap(axisTag, mapping):
            continue
        if mapping and axisTag in normalizedRanges:
            axisRange = normalizedRanges[axisTag]
            mappedMin = floatToFixedToFloat(
                piecewiseLinearMap(axisRange.minimum, mapping), 14
            )
            mappedMax = floatToFixedToFloat(
                piecewiseLinearMap(axisRange.maximum, mapping), 14
            )
            newMapping = {}
            for key, value in mapping.items():
                if key < 0:
                    if axisRange.minimum == 0 or key < axisRange.minimum:
                        continue
                    else:
                        key /= abs(axisRange.minimum)
                elif key > 0:
                    if axisRange.maximum == 0 or key > axisRange.maximum:
                        continue
                    else:
                        key /= axisRange.maximum
                if value < 0:
                    assert mappedMin != 0
                    assert value >= mappedMin
                    value /= abs(mappedMin)
                elif value > 0:
                    assert mappedMax != 0
                    assert value <= mappedMax
                    value /= mappedMax
                key = floatToFixedToFloat(key, 14)
                value = floatToFixedToFloat(value, 14)
                newMapping[key] = value
            newMapping.update({-1.0: -1.0, 1.0: 1.0})
            newSegments[axisTag] = newMapping
        else:
            newSegments[axisTag] = mapping
    varfont["avar"].segments = newSegments


def isInstanceWithinAxisRanges(location, axisRanges):
    for axisTag, coord in location.items():
        if axisTag in axisRanges:
            axisRange = axisRanges[axisTag]
            if coord < axisRange.minimum or coord > axisRange.maximum:
                return False
    return True


def instantiateFvar(varfont, axisLimits):
    # 'axisLimits' dict must contain user-space (non-normalized) coordinates

    location, axisRanges = splitAxisLocationAndRanges(axisLimits, rangeType=AxisRange)

    fvar = varfont["fvar"]

    # drop table if we instantiate all the axes
    if set(location).issuperset(axis.axisTag for axis in fvar.axes):
        log.info("Dropping fvar table")
        del varfont["fvar"]
        return

    log.info("Instantiating fvar table")

    axes = []
    for axis in fvar.axes:
        axisTag = axis.axisTag
        if axisTag in location:
            continue
        if axisTag in axisRanges:
            axis.minValue, axis.maxValue = axisRanges[axisTag]
        axes.append(axis)
    fvar.axes = axes

    # only keep NamedInstances whose coordinates == pinned axis location
    instances = []
    for instance in fvar.instances:
        if any(instance.coordinates[axis] != value for axis, value in location.items()):
            continue
        for axisTag in location:
            del instance.coordinates[axisTag]
        if not isInstanceWithinAxisRanges(instance.coordinates, axisRanges):
            continue
        instances.append(instance)
    fvar.instances = instances


def instantiateSTAT(varfont, axisLimits):
    # 'axisLimits' dict must contain user-space (non-normalized) coordinates

    stat = varfont["STAT"].table
    if not stat.DesignAxisRecord or not (
        stat.AxisValueArray and stat.AxisValueArray.AxisValue
    ):
        return  # STAT table empty, nothing to do

    location, axisRanges = splitAxisLocationAndRanges(axisLimits, rangeType=AxisRange)

    def isAxisValueOutsideLimits(axisTag, axisValue):
        if axisTag in location and axisValue != location[axisTag]:
            return True
        elif axisTag in axisRanges:
            axisRange = axisRanges[axisTag]
            if axisValue < axisRange.minimum or axisValue > axisRange.maximum:
                return True
        return False

    log.info("Instantiating STAT table")

    # only keep AxisValues whose axis is not pinned nor restricted, or is pinned at the
    # exact (nominal) value, or is restricted but the value is within the new range
    designAxes = stat.DesignAxisRecord.Axis
    newAxisValueTables = []
    for axisValueTable in stat.AxisValueArray.AxisValue:
        axisValueFormat = axisValueTable.Format
        if axisValueFormat in (1, 2, 3):
            axisTag = designAxes[axisValueTable.AxisIndex].AxisTag
            if axisValueFormat == 2:
                axisValue = axisValueTable.NominalValue
            else:
                axisValue = axisValueTable.Value
            if isAxisValueOutsideLimits(axisTag, axisValue):
                continue
        elif axisValueFormat == 4:
            # drop 'non-analytic' AxisValue if _any_ AxisValueRecord doesn't match
            # the pinned location or is outside range
            dropAxisValueTable = False
            for rec in axisValueTable.AxisValueRecord:
                axisTag = designAxes[rec.AxisIndex].AxisTag
                axisValue = rec.Value
                if isAxisValueOutsideLimits(axisTag, axisValue):
                    dropAxisValueTable = True
                    break
            if dropAxisValueTable:
                continue
        else:
            log.warn("Unknown AxisValue table format (%s); ignored", axisValueFormat)
        newAxisValueTables.append(axisValueTable)

    stat.AxisValueArray.AxisValue = newAxisValueTables
    stat.AxisValueCount = len(stat.AxisValueArray.AxisValue)


def getVariationNameIDs(varfont):
    used = []
    if "fvar" in varfont:
        fvar = varfont["fvar"]
        for axis in fvar.axes:
            used.append(axis.axisNameID)
        for instance in fvar.instances:
            used.append(instance.subfamilyNameID)
            if instance.postscriptNameID != 0xFFFF:
                used.append(instance.postscriptNameID)
    if "STAT" in varfont:
        stat = varfont["STAT"].table
        for axis in stat.DesignAxisRecord.Axis if stat.DesignAxisRecord else ():
            used.append(axis.AxisNameID)
        for value in stat.AxisValueArray.AxisValue if stat.AxisValueArray else ():
            used.append(value.ValueNameID)
    # nameIDs <= 255 are reserved by OT spec so we don't touch them
    return {nameID for nameID in used if nameID > 255}


@contextmanager
def pruningUnusedNames(varfont):
    origNameIDs = getVariationNameIDs(varfont)

    yield

    log.info("Pruning name table")
    exclude = origNameIDs - getVariationNameIDs(varfont)
    varfont["name"].names[:] = [
        record for record in varfont["name"].names if record.nameID not in exclude
    ]
    if "ltag" in varfont:
        # Drop the whole 'ltag' table if all the language-dependent Unicode name
        # records that reference it have been dropped.
        # TODO: Only prune unused ltag tags, renumerating langIDs accordingly.
        # Note ltag can also be used by feat or morx tables, so check those too.
        if not any(
            record
            for record in varfont["name"].names
            if record.platformID == 0 and record.langID != 0xFFFF
        ):
            del varfont["ltag"]


def setMacOverlapFlags(glyfTable):
    flagOverlapCompound = _g_l_y_f.OVERLAP_COMPOUND
    flagOverlapSimple = _g_l_y_f.flagOverlapSimple
    for glyphName in glyfTable.keys():
        glyph = glyfTable[glyphName]
        # Set OVERLAP_COMPOUND bit for compound glyphs
        if glyph.isComposite():
            glyph.components[0].flags |= flagOverlapCompound
        # Set OVERLAP_SIMPLE bit for simple glyphs
        elif glyph.numberOfContours > 0:
            glyph.flags[0] |= flagOverlapSimple


def normalize(value, triple, avarMapping):
    value = normalizeValue(value, triple)
    if avarMapping:
        value = piecewiseLinearMap(value, avarMapping)
    # Quantize to F2Dot14, to avoid surprise interpolations.
    return floatToFixedToFloat(value, 14)


def normalizeAxisLimits(varfont, axisLimits, usingAvar=True):
    fvar = varfont["fvar"]
    badLimits = set(axisLimits.keys()).difference(a.axisTag for a in fvar.axes)
    if badLimits:
        raise ValueError("Cannot limit: {} not present in fvar".format(badLimits))

    axes = {
        a.axisTag: (a.minValue, a.defaultValue, a.maxValue)
        for a in fvar.axes
        if a.axisTag in axisLimits
    }

    avarSegments = {}
    if usingAvar and "avar" in varfont:
        avarSegments = varfont["avar"].segments
    normalizedLimits = {}
    for axis_tag, triple in axes.items():
        avarMapping = avarSegments.get(axis_tag, None)
        value = axisLimits[axis_tag]
        if isinstance(value, tuple):
            normalizedLimits[axis_tag] = NormalizedAxisRange(
                *(normalize(v, triple, avarMapping) for v in axisLimits[axis_tag])
            )
        else:
            normalizedLimits[axis_tag] = normalize(value, triple, avarMapping)
    return normalizedLimits


def sanityCheckVariableTables(varfont):
    if "fvar" not in varfont:
        raise ValueError("Missing required table fvar")
    if "gvar" in varfont:
        if "glyf" not in varfont:
            raise ValueError("Can't have gvar without glyf")
    # TODO(anthrotype) Remove once we do support partial instancing CFF2
    if "CFF2" in varfont:
        raise NotImplementedError("Instancing CFF2 variable fonts is not supported yet")


def populateAxisDefaults(varfont, axisLimits):
    if any(value is None for value in axisLimits.values()):
        fvar = varfont["fvar"]
        defaultValues = {a.axisTag: a.defaultValue for a in fvar.axes}
        return {
            axisTag: defaultValues[axisTag] if value is None else value
            for axisTag, value in axisLimits.items()
        }
    return axisLimits


def instantiateVariableFont(
    varfont, axisLimits, inplace=False, optimize=True, overlap=True
):
    """ Instantiate variable font, either fully or partially.

    Depending on whether the `axisLimits` dictionary references all or some of the
    input varfont's axes, the output font will either be a full instance (static
    font) or a variable font with possibly less variation data.

    Args:
        varfont: a TTFont instance, which must contain at least an 'fvar' table.
            Note that variable fonts with 'CFF2' table are not supported yet.
        axisLimits: a dict keyed by axis tags (str) containing the coordinates (float)
            along one or more axes where the desired instance will be located.
            If the value is `None`, the default coordinate as per 'fvar' table for
            that axis is used.
            The limit values can also be (min, max) tuples for restricting an
            axis's variation range, but this is not implemented yet.
        inplace (bool): whether to modify input TTFont object in-place instead of
            returning a distinct object.
        optimize (bool): if False, do not perform IUP-delta optimization on the
            remaining 'gvar' table's deltas. Possibly faster, and might work around
            rendering issues in some buggy environments, at the cost of a slightly
            larger file size.
        overlap (bool): variable fonts usually contain overlapping contours, and some
            font rendering engines on Apple platforms require that the `OVERLAP_SIMPLE`
            and `OVERLAP_COMPOUND` flags in the 'glyf' table be set to force rendering
            using a non-zero fill rule. Thus we always set these flags on all glyphs
            to maximise cross-compatibility of the generated instance. You can disable
            this by setting `overalap` to False.
    """
    sanityCheckVariableTables(varfont)

    if not inplace:
        varfont = deepcopy(varfont)

    axisLimits = populateAxisDefaults(varfont, axisLimits)

    normalizedLimits = normalizeAxisLimits(varfont, axisLimits)

    log.info("Normalized limits: %s", normalizedLimits)

    if "gvar" in varfont:
        instantiateGvar(varfont, normalizedLimits, optimize=optimize)

    if "cvar" in varfont:
        instantiateCvar(varfont, normalizedLimits)

    if "MVAR" in varfont:
        instantiateMVAR(varfont, normalizedLimits)

    if "HVAR" in varfont:
        instantiateHVAR(varfont, normalizedLimits)

    if "VVAR" in varfont:
        instantiateVVAR(varfont, normalizedLimits)

    instantiateOTL(varfont, normalizedLimits)

    instantiateFeatureVariations(varfont, normalizedLimits)

    if "avar" in varfont:
        instantiateAvar(varfont, axisLimits)

    with pruningUnusedNames(varfont):
        if "STAT" in varfont:
            instantiateSTAT(varfont, axisLimits)

        instantiateFvar(varfont, axisLimits)

    if "fvar" not in varfont:
        if "glyf" in varfont and overlap:
            setMacOverlapFlags(varfont["glyf"])

    varLib.set_default_weight_width_slant(
        varfont,
        location={
            axisTag: limit
            for axisTag, limit in axisLimits.items()
            if not isinstance(limit, tuple)
        },
    )

    return varfont


def splitAxisLocationAndRanges(axisLimits, rangeType=AxisRange):
    location, axisRanges = {}, {}
    for axisTag, value in axisLimits.items():
        if isinstance(value, rangeType):
            axisRanges[axisTag] = value
        elif isinstance(value, (int, float)):
            location[axisTag] = value
        elif isinstance(value, tuple):
            axisRanges[axisTag] = rangeType(*value)
        else:
            raise TypeError(
                f"Expected number or {rangeType.__name__}, "
                f"got {type(value).__name__}: {value!r}"
            )
    return location, axisRanges


def parseLimits(limits):
    result = {}
    for limitString in limits:
        match = re.match(r"^(\w{1,4})=(?:(drop)|(?:([^:]+)(?:[:](.+))?))$", limitString)
        if not match:
            raise ValueError("invalid location format: %r" % limitString)
        tag = match.group(1).ljust(4)
        if match.group(2):  # 'drop'
            lbound = None
        else:
            lbound = strToFixedToFloat(match.group(3), precisionBits=16)
        ubound = lbound
        if match.group(4):
            ubound = strToFixedToFloat(match.group(4), precisionBits=16)
        if lbound != ubound:
            result[tag] = AxisRange(lbound, ubound)
        else:
            result[tag] = lbound
    return result


def parseArgs(args):
    """Parse argv.

    Returns:
        3-tuple (infile, axisLimits, options)
        axisLimits is either a Dict[str, Optional[float]], for pinning variation axes
        to specific coordinates along those axes (with `None` as a placeholder for an
        axis' default value); or a Dict[str, Tuple(float, float)], meaning limit this
        axis to min/max range.
        Axes locations are in user-space coordinates, as defined in the "fvar" table.
    """
    from fontTools import configLogger
    import argparse

    parser = argparse.ArgumentParser(
        "fonttools varLib.instancer",
        description="Partially instantiate a variable font",
    )
    parser.add_argument("input", metavar="INPUT.ttf", help="Input variable TTF file.")
    parser.add_argument(
        "locargs",
        metavar="AXIS=LOC",
        nargs="*",
        help="List of space separated locations. A location consist in "
        "the tag of a variation axis, followed by '=' and one of number, "
        "number:number or the literal string 'drop'. "
        "E.g.: wdth=100 or wght=75.0:125.0 or wght=drop",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="OUTPUT.ttf",
        default=None,
        help="Output instance TTF file (default: INPUT-instance.ttf).",
    )
    parser.add_argument(
        "--no-optimize",
        dest="optimize",
        action="store_false",
        help="Don't perform IUP optimization on the remaining gvar TupleVariations",
    )
    parser.add_argument(
        "--no-overlap-flag",
        dest="overlap",
        action="store_false",
        help="Don't set OVERLAP_SIMPLE/OVERLAP_COMPOUND glyf flags (only applicable "
        "when generating a full instance)",
    )
    loggingGroup = parser.add_mutually_exclusive_group(required=False)
    loggingGroup.add_argument(
        "-v", "--verbose", action="store_true", help="Run more verbosely."
    )
    loggingGroup.add_argument(
        "-q", "--quiet", action="store_true", help="Turn verbosity off."
    )
    options = parser.parse_args(args)

    infile = options.input
    if not os.path.isfile(infile):
        parser.error("No such file '{}'".format(infile))

    configLogger(
        level=("DEBUG" if options.verbose else "ERROR" if options.quiet else "INFO")
    )

    try:
        axisLimits = parseLimits(options.locargs)
    except ValueError as e:
        parser.error(str(e))

    if len(axisLimits) != len(options.locargs):
        parser.error("Specified multiple limits for the same axis")

    return (infile, axisLimits, options)


def main(args=None):
    infile, axisLimits, options = parseArgs(args)
    log.info("Restricting axes: %s", axisLimits)

    log.info("Loading variable font")
    varfont = TTFont(infile)

    isFullInstance = {
        axisTag for axisTag, limit in axisLimits.items() if not isinstance(limit, tuple)
    }.issuperset(axis.axisTag for axis in varfont["fvar"].axes)

    instantiateVariableFont(
        varfont,
        axisLimits,
        inplace=True,
        optimize=options.optimize,
        overlap=options.overlap,
    )

    outfile = (
        os.path.splitext(infile)[0]
        + "-{}.ttf".format("instance" if isFullInstance else "partial")
        if not options.output
        else options.output
    )

    log.info(
        "Saving %s font %s",
        "instance" if isFullInstance else "partial variable",
        outfile,
    )
    varfont.save(outfile)


if __name__ == "__main__":
    import sys

    sys.exit(main())
