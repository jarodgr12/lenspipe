# EVLA data reduction script for B155+37 (48 spws x 64 channels) in CASA 6.5.7
#
# Must be run with an internet connection to determine antenna offsets and opacity measurements
#



#Calibration steps
thesteps = [0]
step_title = {0: 'Set the variables and initial split (mstransform)',
              1: 'A priori correction of opacity, antenna elevation and antenna positions (gencal)',
              2: 'Flag bad data (flagdata)',
              3: 'Insert model of the flux calibrator (setjy)',
              4: 'Short phase correction (gaincal)',
            m  5: 'Delay correction (gaincal)',
              6: 'Bandpass calibration (bandpass)',
              7: 'Gain (Amplitude and Phase) calibration (gaincal)',
              8: 'Determine the absolute flux-scale of the calibrators (fluxscale)',
              9: 'Applying the calibration tables (applycal)',
              10: 'Split target (split)'}

try:
  print('List of steps to be executed ...'), mysteps
  thesteps = mysteps
except:
  print('global variable mysteps not set.')
if (thesteps==[]):
  thesteps = range(0,len(step_title))
  print('Executing all steps: '), thesteps


# The Python variable 'mysteps' will control which steps
# are executed when you start the script using
#   execfile('scriptForCalibration.py')
# e.g. setting
#   mysteps = [2,3,4]# before starting the script will make the script execute
# only steps 2, 3, and 4
# Setting mysteps = [] will make it execute all steps.

print('Write the value for variables and do a priori flagging -> run the script from the beginning')
#definitions

msfile = '22A-388.sb41674889.eb41704405.59653.561157118056.ms'
myfield = '0,1,3,4' #fields to split
myspw = '0~47' #spw of interest
myscans = '7,11,13,17,18'
mssplit = '22A-388.1555.A.ms' #ms file with the interesting sources
msscans = '22A-388.1555.A.ms.txt' #text file to write listobs output to
mstarget1 = '1555.A.1.ms' #ms file of the target
uvtarget1 = '1555.A.1.uvfits' #uvfits file of the target
#mstarget2 = '2045.A.2.ms' #ms file of the target
#uvtarget2 = '2045.A.2.uvfits' #uvfits file of the target
#mstarget3 = '2045.A.3.ms' #ms file of the target
#uvtarget3 = '2045.A.3.uvfits' #uvfits file of the target
myrefant = 'ea10'  #ea23 or ea28 or ea10, want it to be in the middle 


mystep = 0
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])

#  msfile = raw_input("Msfile, please: ")
#  myfield = raw_input("Fields, please: ")
#  myspw = raw_input("Spectral windows, please: ")
#  mssplit = raw_input("Write the name of the mssplit file, please: ")
#  myTau = raw_input("Opacity parameter, please: ")

  #split(vis=msfile,outputvis= mssplit,datacolumn="data",field=myfield,spw=myspw,width=1,antenna="",timebin="", timerange="",scan=myscans,intent="",array="",uvrange="",correlation="rr,ll",observation="",combine="",keepflags=True,keepmms=False)

  mstransform(
  vis=mymsfile,outputvis=mssplit,createmms=False,separationaxis='auto',numsubms='auto',tileshape=[0],field=myfield,spw=myspw,
  scan=myscans,antenna='',correlation='',timerange='',intent='',array='',uvrange='',observation='',feed='',datacolumn='data',realmodelcol=False,keepflags=True,usewtspectrum=False,combinespws=False,chanaverage=False,chanbin=1,hanning=False,regridms=False,mode='channel',nchan=-1,start=0,width=1,nspw=1,interpolation='linear',phasecenter='',restfreq='',outframe='',veltype='radio',preaverage=False,timeaverage=False,timebin='0s',timespan='',maxuvwdistance=0.0,docallib=False,callib='',douvcontsub=False,fitspw='',
  fitorder=0,want_cont=False,denoising_lib=True,nthreads=1,niter=1,disableparallel=False,ddistart=-1,taql='',monolithic_processing=False,
  reindex=True )


  listobs(vis=mssplit,selectdata=True,spw="",field="",antenna="",uvrange="",timerange="",correlation="",scan="",intent="",feed="",array="",observation="",verbose=True,listfile=msscans,listunfl=False,cachesize=50,overwrite=True)



# step a priori splitting and calibration
mystep = 1
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])

  myTau = plotweather(vis=mssplit, doPlot=True)

  gencal(vis=mssplit,caltable="opacity.cal",caltype="opac",spw="0~47",antenna="",pol="",parameter=myTau)

  gencal(vis=mssplit,caltable="antpos.cal",caltype="antpos",spw="",antenna="",pol="",parameter=[]) # antennas move

  gencal(vis=mssplit,caltable="gaincurve.cal",caltype="gceff",spw="",antenna="",pol="",parameter=[]) # elevation changes 
  
  gencal(vis=mssplit,caltable="rq.cal",caltype="rq") # 3 bit sampling instead of 8, less bits in each vis lets us have more bandwidth

# step flagging
mystep = 2
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])

# pre-calibration flags
# plotms amp vs time/freq/uvdist

# flag zeros
  flagdata(vis=mssplit,mode="clip",autocorr=False,inpfile="",reason="any",tbuff=0.0,spw="",field="",antenna="",uvrange="",timerange="",correlation="",scan="",intent="",array="",observation="",feed="",clipminmax=[],datacolumn="DATA",clipoutside=True,channelavg=False,clipzeros=True,quackinterval=1.0,quackmode="beg",quackincrement=False,tolerance=0.0,addantenna="",lowerlimit=0.0,upperlimit=90.0,ntime="scan",combinescans=False,timecutoff=4.0,freqcutoff=3.0,timefit="line",freqfit="poly",maxnpieces=7,flagdimension="freqtime",usewindowstats="none",halfwin=1,extendflags=True,winsize=3,timedev="",freqdev="",timedevscale=5.0,freqdevscale=5.0,spectralmax=1000000.0,spectralmin=0.0,extendpols=True,growtime=50.0,growfreq=50.0,growaround=False,flagneartime=False,flagnearfreq=False,minrel=0.0,maxrel=1.0,minabs=0,maxabs=-1,spwchan=False,spwcorr=False,basecnt=False,name="Summary",action="apply",display="",flagbackup=True,savepars=False,cmdreason="",outfile="")

# flag dead antennas

# plotms amp vs time
# flag regular off-source

  flagdata(vis=mssplit,mode="manual",autocorr=False,inpfile="",reason="any",tbuff=0.0,spw="23",field="",antenna="1&4;1&5;1&6;1&7",uvrange="",timerange="",correlation="",scan="",intent="",array="",observation="",feed="",clipminmax=[],datacolumn="DATA",clipoutside=True,channelavg=False,clipzeros=False,quackinterval=1.0,quackmode="beg",quackincrement=False,tolerance=0.0,addantenna="",lowerlimit=0.0,upperlimit=90.0,ntime="scan",combinescans=False,timecutoff=4.0,freqcutoff=3.0,timefit="line",freqfit="poly",maxnpieces=7,flagdimension="freqtime",usewindowstats="none",halfwin=1,extendflags=True,winsize=3,timedev="",freqdev="",timedevscale=5.0,freqdevscale=5.0,spectralmax=1000000.0,spectralmin=0.0,extendpols=True,growtime=50.0,growfreq=50.0,growaround=False,flagneartime=False,flagnearfreq=False,minrel=0.0,maxrel=1.0,minabs=0,maxabs=-1,spwchan=False,spwcorr=False,basecnt=False,name="Summary",action="apply",display="",flagbackup=True,savepars=False,cmdreason="",outfile="")

  flagdata(vis=mssplit,mode="quack",autocorr=False,inpfile="",reason="any",tbuff=0.0,spw="",field="2",antenna="",uvrange="",timerange="",correlation="",scan="",intent="",array="",observation="",feed="",clipminmax=[],datacolumn="DATA",clipoutside=True,channelavg=False,clipzeros=False,quackinterval=20,quackmode="beg",quackincrement=False,tolerance=0.0,addantenna="",lowerlimit=0.0,upperlimit=90.0,ntime="scan",combinescans=False,timecutoff=4.0,freqcutoff=3.0,timefit="line",freqfit="poly",maxnpieces=7,flagdimension="freqtime",usewindowstats="none",halfwin=1,extendflags=True,winsize=3,timedev="",freqdev="",timedevscale=5.0,freqdevscale=5.0,spectralmax=1000000.0,spectralmin=0.0,extendpols=True,growtime=50.0,growfreq=50.0,growaround=False,flagneartime=False,flagnearfreq=False,minrel=0.0,maxrel=1.0,minabs=0,maxabs=-1,spwchan=False,spwcorr=False,basecnt=False,name="Summary",action="apply",display="",flagbackup=True,savepars=False,cmdreason="",outfile="")

# Apply after inital calibration

#  flagdata( vis=mssplit,mode='rflag',autocorr=False,inpfile='',reason='any',tbuff=0.0,spw='',field='0,1',antenna='',uvrange='',timerange='',correlation='',scan='',intent='',array='',observation='',feed='',clipminmax=[],datacolumn='corrected',clipoutside=True,channelavg=False,chanbin=1,timeavg=False,timebin='0s',clipzeros=False,quackinterval=1.0,quackmode='beg',quackincrement=False,tolerance=0.0,addantenna='',lowerlimit=0.0,upperlimit=90.0,ntime='scan',combinescans=False,timecutoff=4.0,freqcutoff=3.0,timefit='line',freqfit='poly',maxnpieces=7,flagdimension='freqtime',usewindowstats='none',halfwin=1,extendflags=True,winsize=3,timedev='',freqdev='',timedevscale=5.0,freqdevscale=5.0,spectralmax=1000000.0,spectralmin=0.0,antint_ref_antenna='',minchanfrac=0.6,verbose=False,extendpols=True,growtime=50.0,growfreq=50.0,growaround=False,flagneartime=False,flagnearfreq=False,minrel=0.0,maxrel=1.0,minabs=0,maxabs=-1,spwchan=False,spwcorr=False,basecnt=False,fieldcnt=False,name='Summary',action='apply',display='',flagbackup=True,savepars=False,cmdreason='',outfile='',overwrite=True,writeflags=True )

#  flagdata( vis=mssplit,mode='rflag',autocorr=False,inpfile='',reason='any',tbuff=0.0,spw='',field='0,1',antenna='',uvrange='',timerange='',correlation='',scan='',intent='',array='',observation='',feed='',clipminmax=[],datacolumn='residual',clipoutside=True,channelavg=False,chanbin=1,timeavg=False,timebin='0s',clipzeros=False,quackinterval=1.0,quackmode='beg',quackincrement=False,tolerance=0.0,addantenna='',lowerlimit=0.0,upperlimit=90.0,ntime='scan',combinescans=False,timecutoff=4.0,freqcutoff=3.0,timefit='line',freqfit='poly',maxnpieces=7,flagdimension='freqtime',usewindowstats='none',halfwin=1,extendflags=True,winsize=3,timedev='',freqdev='',timedevscale=5.0,freqdevscale=5.0,spectralmax=1000000.0,spectralmin=0.0,antint_ref_antenna='',minchanfrac=0.6,verbose=False,extendpols=True,growtime=50.0,growfreq=50.0,growaround=False,flagneartime=False,flagnearfreq=False,minrel=0.0,maxrel=1.0,minabs=0,maxabs=-1,spwchan=False,spwcorr=False,basecnt=False,fieldcnt=False,name='Summary',action='apply',display='',flagbackup=True,savepars=False,cmdreason='',outfile='',overwrite=True,writeflags=True )

#  flagdata( vis='22A-388.2045.A.ms',mode='clip',autocorr=False,inpfile='',reason='any',tbuff=0.0,spw='',field='2',antenna='',uvrange='',timerange='',correlation='',scan='',intent='',array='',observation='',feed='',clipminmax=[0.0, 5.0],datacolumn='corrected',clipoutside=True,channelavg=False,chanbin=1,timeavg=False,timebin='0s',clipzeros=False,quackinterval=1.0,quackmode='beg',quackincrement=False,tolerance=0.0,addantenna='',lowerlimit=0.0,upperlimit=90.0,ntime='scan',combinescans=False,timecutoff=4.0,freqcutoff=3.0,timefit='line',freqfit='poly',maxnpieces=7,flagdimension='freqtime',usewindowstats='none',halfwin=1,extendflags=True,winsize=3,timedev='',freqdev='',timedevscale=5.0,freqdevscale=5.0,spectralmax=1000000.0,spectralmin=0.0,antint_ref_antenna='',minchanfrac=0.6,verbose=False,extendpols=True,growtime=50.0,growfreq=50.0,growaround=False,flagneartime=False,flagnearfreq=False,minrel=0.0,maxrel=1.0,minabs=0,maxabs=-1,spwchan=False,spwcorr=False,basecnt=False,fieldcnt=False,name='Summary',action='apply',display='',flagbackup=True,savepars=False,cmdreason='',outfile='',overwrite=True,writeflags=True )

#  flagdata( vis=mssplit,mode='rflag',autocorr=False,inpfile='',reason='any',tbuff=0.0,spw='',field='2',antenna='',uvrange='',timerange='',correlation='',scan='',intent='',array='',observation='',feed='',clipminmax=[],datacolumn='corrected',clipoutside=True,channelavg=False,chanbin=1,timeavg=False,timebin='0s',clipzeros=False,quackinterval=1.0,quackmode='beg',quackincrement=False,tolerance=0.0,addantenna='',lowerlimit=0.0,upperlimit=90.0,ntime='scan',combinescans=False,timecutoff=4.0,freqcutoff=3.0,timefit='line',freqfit='poly',maxnpieces=7,flagdimension='freqtime',usewindowstats='none',halfwin=1,extendflags=True,winsize=3,timedev='',freqdev='',timedevscale=5.0,freqdevscale=5.0,spectralmax=1000000.0,spectralmin=0.0,antint_ref_antenna='',minchanfrac=0.6,verbose=False,extendpols=True,growtime=50.0,growfreq=50.0,growaround=False,flagneartime=False,flagnearfreq=False,minrel=0.0,maxrel=1.0,minabs=0,maxabs=-1,spwchan=False,spwcorr=False,basecnt=False,fieldcnt=False,name='Summary',action='apply',display='',flagbackup=True,savepars=False,cmdreason='',outfile='',overwrite=True,writeflags=True )
    
#   flagdata(vis=mssplit,mode="manual",autocorr=False,inpfile="",reason="any",tbuff=0.0,spw="31",field="2",antenna="10, 21, 22",uvrange="",timerange="",correlation="",scan="",intent="",array="",observation="",feed="",clipminmax=[],datacolumn="DATA",clipoutside=True,channelavg=False,clipzeros=False,quackinterval=1.0,quackmode="beg",quackincrement=False,tolerance=0.0,addantenna="",lowerlimit=0.0,upperlimit=90.0,ntime="scan",combinescans=False,timecutoff=4.0,freqcutoff=3.0,timefit="line",freqfit="poly",maxnpieces=7,flagdimension="freqtime",usewindowstats="none",halfwin=1,extendflags=True,winsize=3,timedev="",freqdev="",timedevscale=5.0,freqdevscale=5.0,spectralmax=1000000.0,spectralmin=0.0,extendpols=True,growtime=50.0,growfreq=50.0,growaround=False,flagneartime=False,flagnearfreq=False,minrel=0.0,maxrel=1.0,minabs=0,maxabs=-1,spwchan=False,spwcorr=False,basecnt=False,name="Summary",action="apply",display="",flagbackup=True,savepars=False,cmdreason="",outfile="")
  
#   flagdata(vis=mssplit,mode="manual",autocorr=False,inpfile="",reason="any",tbuff=0.0,spw="0",field="2",antenna="0&7",uvrange="",timerange="",correlation="",scan="",intent="",array="",observation="",feed="",clipminmax=[],datacolumn="DATA",clipoutside=True,channelavg=False,clipzeros=False,quackinterval=1.0,quackmode="beg",quackincrement=False,tolerance=0.0,addantenna="",lowerlimit=0.0,upperlimit=90.0,ntime="scan",combinescans=False,timecutoff=4.0,freqcutoff=3.0,timefit="line",freqfit="poly",maxnpieces=7,flagdimension="freqtime",usewindowstats="none",halfwin=1,extendflags=True,winsize=3,timedev="",freqdev="",timedevscale=5.0,freqdevscale=5.0,spectralmax=1000000.0,spectralmin=0.0,extendpols=True,growtime=50.0,growfreq=50.0,growaround=False,flagneartime=False,flagnearfreq=False,minrel=0.0,maxrel=1.0,minabs=0,maxabs=-1,spwchan=False,spwcorr=False,basecnt=False,name="Summary",action="apply",display="",flagbackup=True,savepars=False,cmdreason="",outfile="")

# step: model of the flux calibrator-setjy
# observation at 31.7 GHz, using Ka bamd model
mystep = 3
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])

  setjy(vis=mssplit,field="0",spw="",selectdata=False,timerange="",scan="",intent="",observation="",scalebychan=True,standard="Perley-Butler 2010",model="3C286_U.im",listmodels=False,fluxdensity=-1,spix=0.0,reffreq="1GHz",polindex=[],rotmeas=0.0,fluxdict={},useephemdir=False,interpolation="nearest",usescratch=True,ismms=None)


# step: short phase calibration
mystep = 4
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])

  gaincal(vis=mssplit,caltable="intphase.cal",field="0",spw="*:28~36",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="int",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="p",append=False,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal'],gainfield=[],interp=[],spwmap=[],parang=False)


# step: residual delay calibration
mystep = 5
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])
            
  gaincal(vis=mssplit,caltable="delays.cal",field="0",spw="",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="inf",combine="scan",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="K",smodel=[],calmode="p",append=False,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'intphase.cal'],gainfield=[],interp=[],spwmap=[],parang=False)


# step: bandpass calibration
mystep = 6
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])

  bandpass(vis=mssplit,caltable="bpass.cal",field="0",spw="",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="inf",combine="scan",refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,bandtype="B",smodel=[],append=False,fillgaps=0,degamp=3,degphase=3,visnorm=False,maskcenter=0,maskedge=5,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'intphase.cal', 'delays.cal'],gainfield=[],interp=[],spwmap=[],parang=False)


# step: phase & ampltiude calibration
mystep = 7
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])
  
  gaincal(vis=mssplit,caltable="phase.cal",field="0",spw="0:10~53,1~14:4~60,15~16:10~53,17~30:4~60,31~32:10~53,33~46:4~60,47:10~53",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="int",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="p",append=False,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays.cal', 'bpass.cal'],gainfield=[],interp=[],spwmap=[],parang=False)
  
  gaincal(vis=mssplit,caltable="phase.cal",field="1",spw="0:10~53,1~14:4~60,15~16:10~53,17~30:4~60,31~32:10~53,33~46:4~60,47:10~53",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="int",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="p",append=True,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays.cal', 'bpass.cal'],gainfield=[],interp=[],spwmap=[],parang=False)

  gaincal(vis=mssplit,caltable="phase.cal",field="2",spw="0:10~53,1~14:4~60,15~16:10~53,17~30:4~60,31~32:10~53,33~46:4~60,47:10~53",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="int",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="p",append=False,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays.cal', 'bpass.cal'],gainfield=[],interp=[],spwmap=[],parang=False)


  gaincal(vis=mssplit,caltable="amp.cal",field="0",spw="0:10~53,1~14:4~60,15~16:10~53,17~30:4~60,31~32:10~53,33~46:4~60,47:10~53",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="inf",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="ap",append=False,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays.cal', 'bpass.cal', 'phase.cal'],gainfield=['','','','','0','0','0'],interp=['','','','','nearest','nearest','linear'],spwmap=[],parang=False)
  
  gaincal(vis=mssplit,caltable="amp.cal",field="1",spw="0:10~53,1~14:4~60,15~16:10~53,17~30:4~60,31~32:10~53,33~46:4~60,47:10~53",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="inf",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="ap",append=True,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays.cal', 'bpass.cal', 'phase.cal'],gainfield=['','','','','0','0','1'],interp=['','','','','nearest','nearest','linear'],spwmap=[],parang=False)

  gaincal(vis=mssplit,caltable="amp.cal",field="2",spw="0:10~53,1~14:4~60,15~16:10~53,17~30:4~60,31~32:10~53,33~46:4~60,47:10~53",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="inf",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="ap",append=True,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays.cal', 'bpass.cal', 'phase.cal'],gainfield=['','','','','0','0','1'],interp=['','','','','nearest','nearest','linear'],spwmap=[],parang=False)


# step: absolute flux-scale calibration
mystep = 8
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])
            
  flux1 = fluxscale(vis=mssplit,caltable="amp.cal",fluxtable="flux.cal",reference=['0'],transfer=['1'],listfile="",append=False,refspwmap=[-1],gainthreshold=-1.0,antenna="",timerange="",scan="",incremental=True,fitorder=1,display=True)

  setjy(vis=mssplit,field="1",spw="",selectdata=False,timerange="",scan="",intent="",observation="",scalebychan=True,standard="fluxscale",model="",listmodels=False,fluxdensity=-1,spix=0.0,reffreq="1GHz",polindex=[],polangle=[],rotmeas=0.0,fluxdict=flux1,useephemdir=False,interpolation="nearest",usescratch=True,ismms=None)

  gaincal(vis=mssplit,caltable="intphase2.cal",field="1",spw="*:28~36",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="int",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="p",append=False,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal'],gainfield=[],interp=[],spwmap=[],parang=False)

  gaincal(vis=mssplit,caltable="delays2.cal",field="1",spw="",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="inf",combine="scan",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="K",smodel=[],calmode="p",append=False,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'intphase2.cal'],gainfield=[],interp=[],spwmap=[],parang=False)

  bandpass(vis=mssplit,caltable="bpass2.cal",field="1",spw="",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="inf",combine="scan",refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,bandtype="B",smodel=[],append=False,fillgaps=0,degamp=3,degphase=3,visnorm=False,maskcenter=0,maskedge=5,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'intphase2.cal', 'delays2.cal'],gainfield=[],interp=[],spwmap=[],parang=False)

  gaincal(vis=mssplit,caltable="phase2.cal",field="1",spw="0:10~53,1~14:4~60,15~16:10~53,17~30:4~60,31~32:10~53,33~46:4~60,47:10~53",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="int",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="p",append=False,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays2.cal', 'bpass2.cal'],gainfield=[],interp=[],spwmap=[],parang=False)

  gaincal(vis=mssplit,caltable="amp2.cal",field="1",spw="0:10~53,1~14:4~60,15~16:10~53,17~30:4~60,31~32:10~53,33~46:4~60,47:10~53",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",solint="inf",combine="",preavg=-1.0,refant=myrefant,minblperant=4,minsnr=2.0,solnorm=False,gaintype="G",smodel=[],calmode="ap",append=False,splinetime=3600.0,npointaver=3,phasewrap=180.0,docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays2.cal', 'bpass2.cal', 'phase2.cal'],gainfield=[],interp=['','','','','nearest','nearest','linear'],spwmap=[],parang=False)


# step: Application of the calibration tables
mystep = 9
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])

  applycal(vis=mssplit,field="0",spw="",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays.cal', 'bpass.cal', 'phase.cal', 'amp.cal'],gainfield=['','','','','0','0','0','0'],interp=['','','','','nearest','nearest','linear','nearest'],spwmap=[],calwt=False,parang=False,applymode="",flagbackup=True)
  applycal(vis=mssplit,field="1",spw="",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays2.cal', 'bpass2.cal', 'phase2.cal','amp2.cal'],gainfield=['','','','','1','1','1','1'],interp=['','','','','nearest','nearest','linear','nearest'],spwmap=[],calwt=False,parang=False,applymode="",flagbackup=True)
  applycal(vis=mssplit,field="2",spw="",intent="",selectdata=False,timerange="",uvrange="",antenna="",scan="",observation="",msselect="",docallib=False,callib="",gaintable=['antpos.cal', 'gaincurve.cal', 'opacity.cal', 'rq.cal', 'delays2.cal', 'bpass2.cal', 'phase2.cal','amp2.cal'],gainfield=['','','','','1','1','1','1'],interp=['','','','','nearest','nearest','linear','nearest'],spwmap=[],calwt=False,parang=False,applymode="",flagbackup=True)


# step: Split target
mystep = 10
if(mystep in thesteps):
  casalog.post('Step '+str(mystep)+' '+step_title[mystep],'INFO')
  print('Step ', mystep, step_title[mystep])
            
  split(vis=mssplit,outputvis=mstarget1,datacolumn="corrected",field="2",spw="0~15",width=4,antenna="",timebin="",timerange="",scan="",intent="",array="",uvrange="",correlation="",observation="",combine="",keepflags=True,keepmms=False)

  split(vis=mssplit,outputvis=mstarget2,datacolumn="corrected",field="2",spw="16~31",width=4,antenna="",timebin="",timerange="",scan="",intent="",array="",uvrange="",correlation="",observation="",combine="",keepflags=True,keepmms=False)

  split(vis=mssplit,outputvis=mstarget3,datacolumn="corrected",field="2",spw="32~47",width=4,antenna="",timebin="",timerange="",scan="",intent="",array="",uvrange="",correlation="",observation="",combine="",keepflags=True,keepmms=False)

  statwt(vis=mstarget1, minsamp=8, datacolumn='data', flagbackup=False)

  statwt(vis=mstarget1, minsamp=8, datacolumn='data', flagbackup=False)
  
  statwt(vis=mstarget1, minsamp=8, datacolumn='data', flagbackup=False)

  exportuvfits( vis=mstarget1,fitsfile=uvtarget1,datacolumn='data',field='0',spw='',antenna='',timerange='',writesyscal=False,multisource=True,combinespw=True,writestation=True,padwithflags=True,overwrite=False )
  
  exportuvfits( vis=mstarget2,fitsfile=uvtarget2,datacolumn='data',field='0',spw='',antenna='',timerange='',writesyscal=False,multisource=True,combinespw=True,writestation=True,padwithflags=True,overwrite=False )
    
  exportuvfits( vis=mstarget3,fitsfile=uvtarget3,datacolumn='data',field='0',spw='',antenna='',timerange='',writesyscal=False,multisource=True,combinespw=True,writestation=True,padwithflags=True,overwrite=False )

print('Calibration completed')
