#
# LSST Data Management System
#
# Copyright 2008-2016 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#
import math

import lsst.afw.math as afwMath
import lsst.afw.table as afwTable
from lsst.meas.base import BasePlugin
import lsst.meas.algorithms as measAlg
import lsst.pipe.base as pipeBase
from lsstDebug import getDebugFrame
from lsst.afw.display import getDisplay
from lsst.meas.astrom import displayAstrometry

import lsst.pipe.tasks.calibrate

class CfhtCalibrateTask(lsst.pipe.tasks.calibrate.CalibrateTask) :
    @pipeBase.timeMethod
    def run(self, exposure, defects=None, idFactory=None, expId=0):
        """!Run the calibration task on an exposure

        \param[in,out]  exposure   Exposure to calibrate; measured PSF will be installed there as well
        \param[in]      defects    List of defects on exposure
        \param[in]      idFactory  afw.table.IdFactory to use for source catalog.
        \param[in]      expId      Exposure id used for random number generation. Note: Unused
        \return a pipeBase.Struct with fields:
        - exposure: Repaired exposure
        - backgrounds: A list of background models applied in the calibration phase
        - psf: Point spread function
        - sources: Sources used in calibration
        - matches: Astrometric matches
        - matchMeta: Metadata for astrometric matches
        - photocal: Output of photocal subtask

        It is moderately important to provide a decent initial guess for the seeing if you want to
        deal with cosmic rays.  If there's a PSF in the exposure it'll be used; failing that the
        CalibrateConfig.initialPsf is consulted (although the pixel scale will be taken from the
        WCS if available).

        If the exposure contains an lsst.afw.image.Calib object with the exposure time set, MAGZERO
        will be set in the task metadata.
        """
        assert exposure is not None, "No exposure provided"

        if not exposure.hasPsf():
            self.installInitialPsf(exposure)
        if idFactory is None:
            idFactory = afwTable.IdFactory.makeSimple()
        backgrounds = afwMath.BackgroundList()
        keepCRs = True                  # At least until we know the PSF
        self.repair.run(exposure, defects=defects, keepCRs=keepCRs)
        frame = getDebugFrame(self._display, "repair")
        if frame:
            getDisplay(frame).mtv(exposure)

        if self.config.doBackground:
            with self.timer("background"):
                bg, exposure = measAlg.estimateBackground(exposure, self.config.background, subtract=True)
                backgrounds.append(bg)
            frame = getDebugFrame(self._display, "background")
            if frame:
                getDisplay(frame).mtv(exposure)

        # Make both tables from the same detRet, since detection can only be run once
        table1 = afwTable.SourceTable.make(self.schema1, idFactory)
        table1.setMetadata(self.algMetadata)
        detRet = self.detection.makeSourceCatalog(table1, exposure)
        sources1 = detRet.sources
        if detRet.fpSets.background:
            backgrounds.append(detRet.fpSets.background)

        if self.config.doPsf:
            self.initialMeasurement.measure(exposure, sources1)

 # ### Do not compute astrometry before PSF determination. Astrometry will be computed afterwards
 # ###
 #           if self.config.doAstrometry:
 #               astromRet = self.astrometry.run(exposure, sources1)
 #               matches = astromRet.matches
 #           else:
                # If doAstrometry is False, we force the Star Selector to either make them itself
                # or hope it doesn't need them.
 #               matches = None
            matches = None
            psfRet = self.measurePsf.run(exposure, sources1, matches=matches)
            psf = psfRet.psf
        elif exposure.hasPsf():
            psf = exposure.getPsf()
        else:
            psf = None

        # Wash, rinse, repeat with proper PSF

        if self.config.doPsf:
            self.repair.run(exposure, defects=defects, keepCRs=None)
            frame = getDebugFrame(self._display, "PSF_repair")
            if frame:
                getDisplay(frame).mtv(exposure)

        if self.config.doBackground:
            # Background estimation ignores (by default) pixels with the
            # DETECTED bit set, so now we re-estimate the background,
            # ignoring sources.  (see BackgroundConfig.ignoredPixelMask)
            with self.timer("background"):
                # Subtract background
                bg, exposure = measAlg.estimateBackground(
                    exposure, self.config.background, subtract=True,
                    statsKeys=('BGMEAN2', 'BGVAR2'))
                self.log.info("Fit and subtracted background")
                backgrounds.append(bg)

            frame = getDebugFrame(self._display, "PSF_background")
            if frame:
                getDisplay(frame).mtv(exposure)

        # make a second table with which to do the second measurement
        # the schemaMapper will copy the footprints and ids, which is all we need.
        table2 = afwTable.SourceTable.make(self.schema, idFactory)
        table2.setMetadata(self.algMetadata)
        sources = afwTable.SourceCatalog(table2)
        # transfer to a second table -- note that the slots do not have to be reset here
        # as long as measurement.run follows immediately
        sources.extend(sources1, self.schemaMapper)

        if self.config.doMeasureApCorr:
            # Run measurement through all flux measurements (all have the same execution order),
            # then apply aperture corrections, then run the rest of the measurements
            self.measurement.run(exposure, sources, endOrder=BasePlugin.APCORR_ORDER)
            apCorrMap = self.measureApCorr.run(bbox=exposure.getBBox(), catalog=sources).apCorrMap
            exposure.getInfo().setApCorrMap(apCorrMap)
            self.measurement.run(exposure, sources, beginOrder=BasePlugin.APCORR_ORDER)
        else:
            self.measurement.run(exposure, sources)

        if self.config.doAstrometry:
            astromRet = self.astrometry.run(exposure, sources)
            matches = astromRet.matches
            matchMeta = astromRet.matchMeta
        else:
            matches, matchMeta = None, None

        if self.config.doPhotoCal:
            assert(matches is not None)
            try:
                photocalRet = self.photocal.run(exposure, matches)
            except Exception, e:
                self.log.warn("Failed to determine photometric zero-point: %s" % e)
                photocalRet = None
                self.metadata.set('MAGZERO', float("NaN"))

            if photocalRet:
                self.log.info("Photometric zero-point: %f" % photocalRet.calib.getMagnitude(1.0))
                exposure.getCalib().setFluxMag0(photocalRet.calib.getFluxMag0())
                metadata = exposure.getMetadata()
                # convert to (mag/sec/adu) for metadata
                try:
                    magZero = photocalRet.zp - 2.5 * math.log10(exposure.getCalib().getExptime() )
                    metadata.set('MAGZERO', magZero)
                except:
                    self.log.warn("Could not set normalized MAGZERO in header: no exposure time")
                metadata.set('MAGZERO_RMS', photocalRet.sigma)
                metadata.set('MAGZERO_NOBJ', photocalRet.ngood)
                metadata.set('COLORTERM1', 0.0)
                metadata.set('COLORTERM2', 0.0)
                metadata.set('COLORTERM3', 0.0)
        else:
            photocalRet = None

        frame = getDebugFrame(self._display, "calibrate")
        if frame:
            displayAstrometry(exposure=exposure, sourceCat=sources, matches=matches,
                              frame=self.frame, pause=False)

        return pipeBase.Struct(
            exposure = exposure,
            backgrounds = backgrounds,
            psf = psf,
            sources = sources,
            matches = matches,
            matchMeta = matchMeta,
            photocal = photocalRet,
        )