import numpy as np

# Global state
phase = None
filter_state = None
integrator_state = None
start_time = None

TABLE_SIZE = 1024

def onSetupParameters(scriptOp):
    """Define general settings and rebuild pages when needed."""
    general = scriptOp.appendCustomPage('General')

    p = general.appendFloat('Samplerate', label='Sample Rate', order=0)
    p[0].val = 44100

    p = general.appendInt('Numchannels', label='Num Channels', order=1)
    p[0].val = 1; p[0].normMin = 1; p[0].normMax = 100

    p = general.appendInt('Numsamples', label='Num Samples', order=2)
    p[0].val = 735

    p = general.appendFloat('Transitiontime', label='Transition Time', order=3)
    p[0].val = 0.01

    # Create Absolute Time parameter driven by absTime.frame
    p = general.appendFloat('Absolutetime', label='Absolute Time', order=4)
    p[0].expr = 'absTime.frame'; p[0].enableExpr = True

    general.appendPulse('Updatechannels', label='Update Channels', order=5)
    createChannelParameterPages(scriptOp)

def createChannelParameterPages(scriptOp):
    """(Re)build each channel’s page, preserving existing state."""
    global phase, filter_state, integrator_state

    n = scriptOp.par.Numchannels.eval()

    if phase is None:
        phase            = np.zeros(n, dtype=np.float64)
        filter_state     = np.zeros(n, dtype=np.float64)
        integrator_state = np.zeros(n, dtype=np.float64)
    else:
        phase            = np.resize(phase, n)
        filter_state     = np.resize(filter_state, n)
        integrator_state = np.resize(integrator_state, n)

    # Remove surplus pages
    pages = [pg for pg in scriptOp.customPages if pg.name.startswith('Channel ')]
    for pg in pages[n:]:
        pg.destroy()

    # Add or update pages
    pages = [pg for pg in scriptOp.customPages if pg.name.startswith('Channel ')]
    for i in range(len(pages), n):
        letter = chr(ord('a') + i)
        pg = scriptOp.appendCustomPage(f'Channel {i+1}')

        # Frequency & Amplitude at the top
        f = pg.appendFloat(f'Frequency{i}', label=f'Frequency {i+1}', order=0)
        f[0].val = 440; f[0].normMin = 0; f[0].normMax = 10000
        a = pg.appendFloat(f'Amplitude{i}', label=f'Amplitude {i+1}', order=1)
        a[0].val = 1.0

        # Mixer levels
        for idx, mix_type in enumerate(['Sine','Square','Sawtooth','Triangle','Noise','Wavetable']):
            pm = pg.appendFloat(f'{mix_type}mix{letter}', label=f'{mix_type} Mix', order=10+idx)
            pm[0].val = 1.0 if mix_type=='Sine' else 0.0
            pm[0].normMin = 0.0; pm[0].normMax = 1.0

        # Harmonic sliders
        for h in range(16):
            ph = pg.appendFloat(f'Harmonic{h+1}{letter}', label=f'Harmonic {h+1}', order=21+h)
            ph[0].val = 1.0 if h==0 else 0.0
            ph[0].normMin = -1.0; ph[0].normMax = 1.0

        # Offset, Bias, Phase Shift, Smooth Pitch Changes
        off = pg.appendFloat(f'Offset{letter}',  label='Offset', order=40)
        off[0].val = 0.0; off[0].normMin = -1.0; off[0].normMax = 1.0
        bi  = pg.appendFloat(f'Bias{letter}',    label='Bias',   order=41)
        bi[0].val = 0.5; bi[0].normMin = 0.0; bi[0].normMax = 1.0
        phs = pg.appendFloat(f'Phase{letter}',   label='Phase Shift', order=42)
        phs[0].val = 0.0; phs[0].normMin = 0.0; phs[0].normMax = 1.0
        sm  = pg.appendToggle(f'Smooth{letter}', label='Smooth Pitch Changes', order=43)
        sm[0].val = False

def onPulse(par):
    """Rebuild pages next cook when Update Channels is pressed."""
    if par.name == 'Updatechannels':
        pass

def onStart(scriptOp):
    """Initialise timing and pages before the first cook."""
    global start_time
    start_time = absTime.seconds
    createChannelParameterPages(scriptOp)

def poly_blep(t, dt):
    """Vectorised BLEP for anti-aliasing."""
    out = np.zeros_like(t)
    m1 = t < dt
    m2 = t > 1 - dt
    ti = t[m1] / dt
    out[m1] = ti + ti - ti*ti - 1
    tj = (t[m2] - 1) / dt
    out[m2] = tj*tj + tj + tj + 1
    return out

def rebuild_wavetable(i, harmonics):
    """Build and normalise wavetable from 16 harmonics."""
    phs = np.linspace(0, 1, TABLE_SIZE, endpoint=False)
    table = sum(g * np.sin(2*np.pi*(h+1)*phs) for h, g in enumerate(harmonics))
    m = np.max(np.abs(table))
    return table/m if m>0 else table

def generate_waveforms_block(length, sr, phase, freqs, amps,
                             mixer_levels, offsets, biases,
                             phase_shifts, smooth_flags, all_harmonics):
    """Vectorised generation for all channels at once."""
    nch = len(phase)
    out = np.zeros((nch, length))
    new_phase = np.zeros_like(phase)
    new_int   = np.zeros_like(phase)

    for i in range(nch):
        ph0 = phase[i]
        inc = freqs[i] / sr
        idxs = ph0 + inc * np.arange(length)
        ti = idxs % 1.0

        sine = np.sin(2*np.pi*ti)
        square = np.where(ti < biases[i], 1.0, -1.0)
        square -= poly_blep(ti, inc)
        square += poly_blep((ti + biases[i]) % 1.0, inc)

        saw = 2*ti - 1
        saw -= poly_blep(ti, inc)

        tri_int = integrator_state[i]
        tri = np.empty(length)
        y = tri_int
        for n, sqv in enumerate(square):
            y = 0.995*y + inc*sqv
            tri[n] = 4*y
        new_int[i] = y

        noise = np.random.uniform(-1, 1, length)

        harms = all_harmonics[i]
        table = rebuild_wavetable(i, harms)
        idxf = (ti * TABLE_SIZE) % TABLE_SIZE
        i0 = idxf.astype(int)
        frac = idxf - i0
        wt = (1-frac)*table[i0] + frac*table[(i0+1)%TABLE_SIZE]

        mix = ( mixer_levels[i,0]*sine +
                mixer_levels[i,1]*square +
                mixer_levels[i,2]*saw +
                mixer_levels[i,3]*tri +
                mixer_levels[i,4]*noise +
                mixer_levels[i,5]*wt )

        out[i] = mix * amps[i] + offsets[i]
        new_phase[i] = idxs[-1] % 1.0

    return out, new_phase, new_int

def apply_one_pole(signals, sr, state, cutoff=8000.0):
    """Per-channel one-pole smoothing."""
    alpha = np.exp(-2*np.pi*cutoff/sr)
    out = np.empty_like(signals)
    for i in range(signals.shape[0]):
        y = state[i]
        seq = signals[i]
        ys = np.empty_like(seq)
        for n, v in enumerate(seq):
            y = alpha*y + (1-alpha)*v
            ys[n] = y
        out[i] = ys
        state[i] = y
    return out

def onCook(scriptOp):
    """Main audio callback with guard for pages and numSamples override."""
    global phase, filter_state, integrator_state, start_time

    # Guard: rebuild pages if needed
    if (phase is None
        or len(phase) != scriptOp.par.Numchannels.eval()
        or scriptOp.par.Updatechannels.eval()):
        scriptOp.par.Updatechannels.val = 0
        createChannelParameterPages(scriptOp)

    # Override built-in samples count
    numS = scriptOp.par.Numsamples.eval()
    scriptOp.isTimeSlice = False
    scriptOp.numSamples = int(numS)

    sr    = scriptOp.par.Samplerate.eval()
    trans = scriptOp.par.Transitiontime.eval()
    chans = scriptOp.par.Numchannels.eval()
    now   = absTime.seconds

    if start_time is None:
        start_time = now

    length = numS
    scriptOp.isTimeSlice = (now - start_time) >= trans

    freqs = np.array([scriptOp.par[f'Frequency{i}'].eval() for i in range(chans)], float)
    amps  = np.array([scriptOp.par[f'Amplitude{i}'].eval()  for i in range(chans)], float)
    mixer = np.array([[scriptOp.par[f'{mt}mix{chr(ord("a")+i)}'].eval()
                       for mt in ['Sine','Square','Sawtooth','Triangle','Noise','Wavetable']]
                      for i in range(chans)], float)
    offsets      = np.array([scriptOp.par[f'Offset{chr(ord("a")+i)}'].eval() for i in range(chans)], float)
    biases       = np.array([scriptOp.par[f'Bias{chr(ord("a")+i)}'].eval()   for i in range(chans)], float)
    phase_shifts = np.array([scriptOp.par[f'Phase{chr(ord("a")+i)}'].eval()  for i in range(chans)], float)
    smooth_flags = np.array([scriptOp.par[f'Smooth{chr(ord("a")+i)}'].eval() for i in range(chans)], bool)
    all_harmonics = np.array([[scriptOp.par[f'Harmonic{h+1}{chr(ord("a")+i)}'].eval()
                               for h in range(16)] for i in range(chans)], float)

    raw, phase, integrator_state = generate_waveforms_block(
        length, sr, phase, freqs, amps,
        mixer, offsets, biases,
        phase_shifts, smooth_flags, all_harmonics
    )

    smooth = apply_one_pole(raw, sr, filter_state, cutoff=8000.0)

    scriptOp.clear()
    scriptOp.rate = sr
    for i in range(chans):
        ch = scriptOp.appendChan(f'oscillator_{i+1}')
        ch.vals = smooth[i].astype(np.float32)
