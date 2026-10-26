"""Conservative, replay-time classification; never run on the capture thread."""
SEA_HP_TICK = "nightmare_sea_periodic_hp_loss"

def recovery_windows(windows, heals):
    """Include proven short post-encounter restores, never bridge into next output.

    Observed black-dragon completion delays are 5--22 seconds. A bounded
    30-second grace applies only to identified system recovery, not arbitrary
    later player healing. Death still terminates responsibility independently.
    """
    ordered = sorted(windows)
    times = [e.timestamp_ms / 1000 for e in heals
             if e.attribution_method == "completion_recovery_unattributed"]
    result = []
    for i, (start, end) in enumerate(ordered):
        next_start = ordered[i+1][0] if i+1 < len(ordered) else float('inf')
        trailing = [t for t in times if end < t <= end+30 and t < next_start]
        result.append((start, max([end, *trailing])))
    return tuple(result)

def sea_tick_ids(events, *, sea_present):
    """Require three consecutive synchronized party ticks, not amount alone.

    Packet gaps break a run. Single-player damage and other nests stay unclassified.
    Returned IDs refer to input events, with no invented or filled-in ticks.
    """
    if not sea_present:
        return set()
    rows = sorted((e for e in events if e.amount == 10000
                   and e.raw_attribution_method == "s2c-udp-player-hp-delta-1c1b0005-v576"),
                  key=lambda e:e.timestamp_ms)
    waves = []
    for e in rows:
        if not waves or e.timestamp_ms - waves[-1][0].timestamp_ms > 150:
            waves.append([])
        waves[-1].append(e)
    runs = []; run = []
    for wave in waves:
        valid = len({e.target_id for e in wave}) >= 3
        if not valid or (run and not 1700 <= wave[0].timestamp_ms-run[-1][0].timestamp_ms <= 2400):
            if len(run) >= 3: runs.extend(run)
            run = []
        if valid: run.append(wave)
    if len(run) >= 3: runs.extend(run)
    confirmed = {id(e) for wave in runs for e in wave}
    # Party recipients are not always delivered in the same 150 ms burst.
    # Extend only an uninterrupted per-recipient periodic run which intersects
    # an independently confirmed synchronized party run. Never fill missing ticks.
    by_target = {}
    for event in rows:
        by_target.setdefault(event.target_id, []).append(event)
    chains = []
    for target_rows in by_target.values():
        chain = []
        for event in target_rows:
            if chain and not 1700 <= event.timestamp_ms-chain[-1].timestamp_ms <= 2400:
                if len(chain) >= 3:
                    chains.append(chain)
                if len(chain) >= 3 and any(id(e) in confirmed for e in chain):
                    confirmed.update(id(e) for e in chain)
                chain = []
            chain.append(event)
        if len(chain) >= 3 and any(id(e) in confirmed for e in chain):
            confirmed.update(id(e) for e in chain)
        if len(chain) >= 3:
            chains.append(chain)
    # Death/re-entry can restart one recipient's tick phase. Require their own
    # three periodic ticks AND concurrent confirmed periodic ticks on at least
    # two other recipients; do not require identical phase after re-entry.
    confirmed_rows = [e for e in rows if id(e) in confirmed]
    for chain in chains:
        peers = {}
        for event in confirmed_rows:
            if (event.target_id != chain[0].target_id
                    and chain[0].timestamp_ms-150 <= event.timestamp_ms <= chain[-1].timestamp_ms+150):
                peers[event.target_id] = peers.get(event.target_id, 0)+1
        if sum(count >= 2 for count in peers.values()) >= 2:
            confirmed.update(id(e) for e in chain)
    for wave in waves:
        if len({e.target_id for e in wave}) >= 3 and any(id(e) in confirmed for e in wave):
            confirmed.update(id(e) for e in wave)
    return confirmed
