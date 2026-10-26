from __future__ import annotations

from functools import lru_cache

class V576ProfileDecoderMixin:
    def _observe_profile_probe(
        self,
        packet: PacketRecord,
        decoded: DecodedV576Message | None,
        *,
        _glyph_refresh: bool = False,
    ) -> PlayerInspectEvent | None:
        """Pair a passive character-inspect request with its server response.

        A target actor already proven elsewhere in the same session may be
        linked from the request. The calibrated response fields are mapped
        through the v576 item/enchant catalog and never alter DPS totals.
        """
        # Team inspection uses a verified encrypted 11-byte request. Do not
        # treat every 11-byte message as an inspect request: normal gameplay
        # also emits unrelated messages of that size and pairing one of those
        # with a later profile-shaped response can open the teammate overlay
        # without the user viewing a character. Opening the local status panel
        # uses a separately encrypted 44-byte request. TCP receive offload can
        # append a small state message to the large response, so inspect each
        # length-framed application message independently.
        if packet.transport.upper() != "TCP":
            return None
        direction = packet.inferred_direction()
        message_payloads = _split_length_framed_tcp_messages(packet.payload)
        if direction == "client_to_server":
            # Requests without a matching profile body used to leave their
            # inspection-key side table behind for the lifetime of the ACT
            # process.  Clean both request structures on every later client
            # packet, not only when another large profile response arrives.
            request_cutoff = (
                packet.timestamp_seconds - _PROFILE_RESPONSE_WINDOW_SECONDS
            )
            while (
                self.profile_probe_requests
                and self.profile_probe_requests[0][0] < request_cutoff
            ):
                stale_request = self.profile_probe_requests.popleft()
                self.profile_probe_inspection_keys.pop(stale_request[1], None)
            session_cutoff = packet.timestamp_seconds - 30.0
            for profile_id, session in tuple(
                self.profile_inspection_sessions.items()
            ):
                if session[0] < session_cutoff:
                    self.profile_inspection_sessions.pop(profile_id, None)
            pending_inspect_event = None
            for payload in message_payloads:
                glyph_page_request = decode_tcp_glyph_page_request_payload(payload)
                if glyph_page_request is not None:
                    # A request is intent only.  The page becomes active after
                    # the server's 03 1D acknowledgement, never optimistically.
                    self.pending_glyph_page_request = glyph_page_request.page_index
                    continue
                party_request = decode_tcp_party_create_request_payload(payload)
                if party_request is not None and party_request.owner_name:
                    # A default party title carries authoritative local name
                    # text; the following server party ID is newly allocated.
                    self.local_name = party_request.owner_name
                inspect_request = decode_tcp_inspect_request_payload(payload)
                # 17 01 is emitted at the actual click and is followed by a
                # structurally verified 17 00 identity response. 03 15 can be
                # delayed by several seconds while the game opens its own UI,
                # so waiting for it made ACT appear progressively slower.
                # 03 15 remains the only request paired with the large profile
                # body; both stages share one stable inspection key.
                is_inspect_open = bool(
                    inspect_request is not None
                    and inspect_request.opcode == b"\x17\x01"
                )
                is_inspect_details = bool(
                    inspect_request is not None
                    and inspect_request.opcode == b"\x03\x15"
                )
                is_inspect_close = bool(
                    inspect_request is not None
                    and inspect_request.opcode == b"\x07\x3d"
                )
                self_status_actor_id = _self_status_request_actor_id(payload)
                is_self_status = self_status_actor_id is not None
                if is_inspect_close and inspect_request is not None:
                    closed_session = self.profile_inspection_sessions.pop(
                        inspect_request.profile_id, None
                    )
                    if closed_session is not None:
                        closed_key = closed_session[1]
                        for fingerprint, key in tuple(
                            self.profile_probe_inspection_keys.items()
                        ):
                            if key == closed_key:
                                self.profile_probe_inspection_keys.pop(
                                    fingerprint, None
                                )
                    continue
                is_team_inspect = is_inspect_open or is_inspect_details
                if not (is_team_inspect or is_self_status):
                    continue
                request_packet = replace(packet, payload=payload)
                request_fingerprint = _wire_fingerprint(request_packet)
                inspection_key = ""
                existing_session = None
                if inspect_request is not None:
                    existing_session = self.profile_inspection_sessions.get(
                        inspect_request.profile_id
                    )
                    if (
                        is_inspect_open
                        or existing_session is None
                        or packet.timestamp_seconds - existing_session[0] > 30.0
                    ):
                        inspection_key = (
                            f"inspect-profile:{inspect_request.profile_id:08X}:"
                            f"{round(packet.timestamp_seconds * 1000)}"
                        )
                        self.profile_inspection_sessions[
                            inspect_request.profile_id
                        ] = (packet.timestamp_seconds, inspection_key)
                    else:
                        inspection_key = existing_session[1]
                if is_inspect_details or is_self_status:
                    self.profile_probe_requests.append(
                        (
                            packet.timestamp_seconds,
                            request_fingerprint,
                            request_packet,
                        )
                    )
                    if inspection_key:
                        self.profile_probe_inspection_keys[
                            request_fingerprint
                        ] = inspection_key
                if is_team_inspect and inspect_request is not None:
                    # Open the lightweight history/simulation shell at 17 01.
                    # A standalone 03 15 still opens it for compatibility with
                    # captures that started after the identity exchange.
                    target_name, target_job = self._actor_display_context(
                        inspect_request.profile_id
                    )
                    origin = self.first_timestamp or packet.timestamp_seconds
                    # Do not publish a second pending revision when 03 15 is
                    # merely continuing the 17 01 operation already shown.
                    if is_inspect_open or existing_session is None:
                        pending_inspect_event = PlayerInspectEvent(
                            timestamp_ms=max(
                                0,
                                round(
                                    (packet.timestamp_seconds - origin) * 1000
                                ),
                            ),
                            target_id=(
                                f"profile:0x{inspect_request.profile_id:08X}"
                            ),
                            target_name=target_name,
                            job=target_job,
                            status="已识别查看信息操作，装备正文解析中",
                            inspection_key=inspection_key,
                            request_fingerprint=request_fingerprint,
                            response_layout="TCP:inspect_request:pending",
                            fields=(
                                ("响应类型", "查看信息"),
                                (
                                    "角色档案标识",
                                    f"0x{inspect_request.profile_id:08X}",
                                ),
                                (
                                    "请求阶段",
                                    "17 01 身份请求"
                                    if is_inspect_open
                                    else "03 15 装备请求",
                                ),
                                ("装备正文", "等待服务器响应"),
                            ),
                        )
                if self_status_actor_id is not None:
                    self.local_actor_ids.add(self_status_actor_id)
                    self.current_self_actor_id = self_status_actor_id
                    target_name, target_job = self._actor_display_context(
                        self_status_actor_id
                    )
                    if (
                        self.pending_self_profile is not None
                        or self.pending_self_item_archive is not None
                    ):
                        profile = (
                            self.pending_self_profile
                            or self.pending_self_item_archive
                        )
                        self.pending_self_profile = None
                        self.pending_self_item_archive = None
                        origin = self.first_timestamp or packet.timestamp_seconds
                        linked_profile = replace(
                            profile,
                            timestamp_ms=max(
                                0,
                                round(
                                    (packet.timestamp_seconds - origin) * 1000
                                ),
                            ),
                            target_id=f"0x{self_status_actor_id:08X}",
                            target_name=target_name or profile.target_name,
                            job=target_job or profile.job,
                            status="已关联本人状态栏与登录装备档案",
                            fields=profile.fields
                            + (
                                ("响应类型", "自身状态栏"),
                                ("实时ID", f"0x{self_status_actor_id:08X}"),
                            ),
                        )
                        self.latest_self_profile = linked_profile
                        return linked_profile
                    origin = self.first_timestamp or packet.timestamp_seconds
                    return PlayerInspectEvent(
                        timestamp_ms=max(
                            0,
                            round((packet.timestamp_seconds - origin) * 1000),
                        ),
                        target_id=f"0x{self_status_actor_id:08X}",
                        target_name=target_name,
                        job=target_job,
                        status=(
                            "已识别自身状态栏；装备页来自登录阶段的客户端缓存，"
                            "打开状态栏本身不会重新下发完整物品档案"
                        ),
                        request_fingerprint=_wire_fingerprint(request_packet),
                        response_layout="TCP:self_status_request:no_profile_payload",
                        fields=(
                            ("响应类型", "自身状态栏"),
                            ("实时ID", f"0x{self_status_actor_id:08X}"),
                            ("物品档案", "本次记录未包含登录阶段档案"),
                            (
                                "采集说明",
                                "请在登录角色前开始实时统计，进入角色后再打开自身状态栏",
                            ),
                        ),
                        equipment=(),
                        raw_payload_hex="",
                    )
            return pending_inspect_event
        if direction != "server_to_client":
            return None
        for payload in message_payloads:
            glyph_page_state = decode_tcp_glyph_page_state_payload(
                payload,
                connection_context=packet,
            )
            if glyph_page_state is None:
                continue
            self.active_glyph_page_by_actor[glyph_page_state.actor_id] = (
                glyph_page_state.page_index
            )
            pending_page = self.pending_glyph_page_request
            is_local_glyph_state = bool(
                glyph_page_state.actor_id == self.current_self_actor_id
                or glyph_page_state.actor_id in self.local_actor_ids
                or (
                    pending_page is not None
                    and pending_page == glyph_page_state.page_index
                )
                or (
                    self.self_login_profile_packet is not None
                    and self.current_login_glyph_actor_id
                    in {None, glyph_page_state.actor_id}
                )
            )
            if is_local_glyph_state:
                self.current_login_glyph_actor_id = glyph_page_state.actor_id
                self.current_login_glyph_page = glyph_page_state.page_index
            if (
                pending_page is not None
                and pending_page == glyph_page_state.page_index
            ):
                self.pending_glyph_page_request = None
            # 07 03 contains both saved pages and normally arrives just before
            # the initial 03 1D state. Re-render the same immutable archive so
            # a page switch updates the visible profile without waiting for a
            # second equipment response.
            if (
                is_local_glyph_state
                and self.self_login_profile_packet is not None
                and not _glyph_refresh
            ):
                previous_profile = self.latest_self_profile
                refreshed = self._observe_profile_probe(
                    self.self_login_profile_packet,
                    None,
                    _glyph_refresh=True,
                )
                if refreshed is not None:
                    if previous_profile is not None:
                        supplement = self._glyph_refresh_supplement(
                            previous_profile
                        )
                        if (
                            supplement.equipment
                            or supplement.cards
                            or supplement.fields
                        ):
                            refreshed = self._merge_inspect_supplement(
                                refreshed,
                                supplement,
                            )
                    target_name, target_job = self._actor_display_context(
                        glyph_page_state.actor_id
                    )
                    origin = self.first_timestamp or packet.timestamp_seconds
                    refreshed = replace(
                        refreshed,
                        timestamp_ms=max(
                            0,
                            round((packet.timestamp_seconds - origin) * 1000),
                        ),
                        target_id=f"0x{glyph_page_state.actor_id:08X}",
                        target_name=target_name or refreshed.target_name,
                        job=target_job or refreshed.job,
                        status="已按服务器确认的当前纹章页刷新完整装备档案",
                    )
                    self.pending_self_profile = refreshed
                    self.latest_self_profile = refreshed
                    return refreshed
        for payload in message_payloads:
            identity = decode_tcp_inspect_identity_payload(payload)
            if identity is not None:
                self.profile_identity_confirmations[identity.profile_id] = (
                    identity.identity_flags,
                    identity.profile_marker,
                )
        response_payload = next(
            (
                payload
                for payload in message_payloads
                if len(payload) >= _PROFILE_RESPONSE_MIN_LENGTH
                and is_tcp_profile_payload_shape(payload)
            ),
            None,
        )
        if response_payload is None:
            return None
        response_packet = replace(packet, payload=response_payload)
        profile_diagnostic = diagnose_tcp_profile_payload(
            response_packet.payload,
            record_unknown=False,
        )
        decoded_profile = profile_diagnostic.decoded
        cutoff = packet.timestamp_seconds - _PROFILE_RESPONSE_WINDOW_SECONDS
        while (
            self.profile_probe_requests
            and self.profile_probe_requests[0][0] < cutoff
        ):
            stale_request = self.profile_probe_requests.popleft()
            self.profile_probe_inspection_keys.pop(stale_request[1], None)
        matching_requests = [
            row
            for row in self.profile_probe_requests
            if row[0] <= packet.timestamp_seconds
            and _reverse_flow_matches(row[2], response_packet)
        ]
        passive_profile = not matching_requests and decoded_profile is not None
        if not matching_requests and not passive_profile:
            return None
        if matching_requests:
            matched_request = matching_requests[-1]
            request_time, request_fingerprint, request_packet = matched_request
            self.profile_probe_requests.remove(matched_request)
            if decoded_profile is None:
                matched_inspect_request = decode_tcp_inspect_request_payload(
                    request_packet.payload
                )
                profile_diagnostic = diagnose_tcp_profile_payload(
                    response_packet.payload,
                    record_unknown=True,
                    expected_profile_id=(
                        matched_inspect_request.profile_id
                        if matched_inspect_request is not None
                        else 0
                    ),
                )
        else:
            request_time = packet.timestamp_seconds
            request_fingerprint = (
                f"passive-profile:{decoded_profile.profile_id:08X}"
            )
            request_packet = replace(response_packet, payload=b"")
        inspect_request = decode_tcp_inspect_request_payload(request_packet.payload)
        request_profile_id = (
            inspect_request.profile_id if inspect_request is not None else 0
        )
        inspection_key = self.profile_probe_inspection_keys.pop(
            request_fingerprint,
            (
                f"{_packet_content_fingerprint(request_packet)}:"
                f"{round(request_time * 1000)}"
            ),
        )
        self_status_actor_id = _self_status_request_actor_id(
            request_packet.payload
        )
        response_fingerprint = _packet_content_fingerprint(response_packet)
        response_key = (request_fingerprint, response_fingerprint)
        previous = self.profile_probe_responses.get(response_key)
        if (
            not _glyph_refresh
            and previous is not None
            and packet.timestamp_seconds - previous < 3.0
        ):
            return None
        self.profile_probe_responses[response_key] = packet.timestamp_seconds
        if len(self.profile_probe_responses) > 256:
            # Long-running clients may inspect hundreds of changing equipment
            # bodies. Only the recent three-second dedupe window matters.
            self.profile_probe_responses.pop(next(iter(self.profile_probe_responses)))
        strings: tuple[str, ...] = ()
        fields: tuple[tuple[str, str], ...] = (
            ("传输", response_packet.transport.upper()),
            ("响应字节", str(len(response_packet.payload))),
            ("响应延迟", f"{max(0.0, packet.timestamp_seconds - request_time):.3f}s"),
        )
        equipment: tuple[tuple[str, str], ...] = ()
        decoded_job = ""
        if decoded_profile is not None:
            resolved_records: list[tuple[str, str]] = []
            mapped_count = 0
            displayable_count = 0
            job_evidence: Counter[tuple[str, int]] = Counter()
            skill_plate_names: list[str] = []
            profile_metadata: list[tuple[str, str]] = []
            equipped_set_items: list[tuple[str, object]] = []
            glyph_groups = sorted({
                record.group_index
                for record in decoded_profile.records
                if record.group_index >= 2
                and inspect_record_category(
                    record.group_index,
                    self.inspect_item_catalog.get(record.template_id),
                ) == "纹章"
            })
            # A 07 03 archive contains both saved heraldry pages.  Group order
            # alone never identifies the active page: only the independent,
            # authoritative server 03 1D state may select glyph_groups[index].
            # If that state is absent, fail closed instead of applying page 1
            # or the final group and silently changing the inspected stats.
            (
                active_glyph_group,
                active_glyph_page_index,
                glyph_page_source,
            ) = self._resolve_authoritative_glyph_group(
                glyph_groups,
                passive_profile=passive_profile,
                decoded_profile_id=decoded_profile.profile_id,
                request_profile_id=request_profile_id,
                self_status_actor_id=self_status_actor_id,
            )
            for record in decoded_profile.records:
                if record.group_index == 0 and record.record_index in {0, 1}:
                    if record.template_id > 0:
                        profile_metadata.append(
                            (
                                "角色外观模板"
                                if record.record_index == 0
                                else "发型外观模板",
                                f"0x{record.template_id:08X}",
                            )
                        )
                    continue
                if record.template_id <= 0:
                    continue
                item = self.inspect_item_catalog.get(record.template_id)
                category = inspect_record_category(record.group_index, item)
                if (
                    category == "纹章"
                    and glyph_groups
                    and (
                        active_glyph_group is None
                        or record.group_index != active_glyph_group
                    )
                ):
                    continue
                displayable_count += 1
                if item is not None:
                    mapped_count += 1
                    item_text = f"{item.name} {item.description}"
                    if "技能纹章" in item_text:
                        plate_match = re.search(r"[（(]([^（）()]+)[）)]", item_text)
                        if plate_match is not None:
                            skill_plate_names.append(plate_match.group(1).strip())
                    for job_code in item.job_class_codes:
                        job_row = self.inspect_job_codes.get(job_code)
                        if job_row is not None:
                            job_evidence[job_row] += 1
                slot = inspect_record_slot(
                    record.group_index,
                    record.record_index,
                    category,
                    item,
                )
                if item is not None and category == "装备":
                    equipped_set_items.append((slot, item))
                engraving_extra_resolved: bool | None = None
                engraving_extra = None
                if category == "刻印":
                    engraving_extra_resolved, engraving_extra = (
                        self._engraving_extra_resolution(record)
                    )
                (
                    potential_attributes_resolved,
                    potential_attributes,
                    potential_option_code,
                ) = self._potential_resolution(item, record)
                (
                    inherent_attributes_resolved,
                    inherent_attributes,
                    inherent_option_code,
                ) = self._type_param1_resolution(item, record, category)
                (
                    suffix_resolved,
                    suffix_name,
                    suffix_attributes,
                    suffix_option_code,
                ) = self._type_param3_resolution(item, record, category)
                glyph_key = (
                    (item.glyph_potential_id, record.field_29)
                    if item is not None
                    and category == "纹章"
                    and item.glyph_potential_id > 0
                    and record.field_29 > 0
                    else None
                )
                glyph_attributes_resolved = (
                    glyph_key in self.inspect_glyph_potential_catalog
                    if glyph_key is not None
                    else None
                )
                core_rune_key = (
                    (
                        item.level,
                        record.core_rune_param_id,
                        record.core_rune_coefficient_rank,
                    )
                    if item is not None and "源铸" in item.name
                    else None
                )
                core_rune_value = (
                    self.inspect_core_rune_catalog.get(core_rune_key)
                    if core_rune_key is not None
                    else None
                )
                resolved_records.append(
                    (
                        f"{category}|{slot}",
                        inspect_record_detail(
                            record.template_id,
                            record.enhance_level,
                            item,
                            self.inspect_enchant_catalog.get(
                                (
                                    item.enchant_id if item is not None else 0,
                                    record.enhance_level,
                                ),
                                (),
                            ),
                            record.dynamic_attributes,
                            potential_attributes,
                            potential_attributes_resolved=(
                                potential_attributes_resolved
                            ),
                            potential_option_code=potential_option_code,
                            inherent_attributes=inherent_attributes,
                            inherent_attributes_resolved=(
                                inherent_attributes_resolved
                            ),
                            inherent_option_code=inherent_option_code,
                            inherent_label=(
                                "首饰固有属性"
                                if record.group_index == 0
                                and 7 <= record.record_index <= 10
                                else "实例固有属性"
                            ),
                            glyph_attributes=(
                                self.inspect_glyph_potential_catalog.get(
                                    glyph_key,
                                    (),
                                )
                                if glyph_key is not None
                                else ()
                            ),
                            glyph_attributes_resolved=glyph_attributes_resolved,
                            glyph_option_code=record.field_29,
                            engraving_extra_attributes=(
                                (engraving_extra[0],)
                                if engraving_extra is not None else ()
                            ),
                            engraving_extra_resolved=engraving_extra_resolved,
                            engraving_extra_selector=(
                                record.engraving_extra_selector
                                if category == "刻印" else None
                            ),
                            accessory_variant=record.accessory_variant,
                            suffix_index=record.suffix_index,
                            suffix_name=suffix_name,
                            suffix_attributes=suffix_attributes,
                            suffix_resolved=suffix_resolved,
                            suffix_option_code=suffix_option_code,
                            core_rune_param_id=record.core_rune_param_id,
                            core_rune_coefficient_rank=(
                                record.core_rune_coefficient_rank
                            ),
                            core_rune_resolved=(
                                core_rune_key in self.inspect_core_rune_catalog
                                if core_rune_key is not None
                                else None
                            ),
                            core_rune_detail=(
                                inspect_core_rune_text(core_rune_value)
                                if core_rune_value is not None
                                else ""
                            ),
                            attribute_multiplier=(
                                inspect_talisman_slot_multiplier(
                                    record.record_index
                                )
                                if category == "护符"
                                else 1.0
                            ),
                        ),
                    )
                )
            direct_job = ""
            direct_job_index = 99
            if job_evidence:
                best_stage = min(job_index for _job_name, job_index in job_evidence)
                direct_job = max(
                    (
                        (count, job_name)
                        for (job_name, job_index), count in job_evidence.items()
                        if job_index == best_stage
                    ),
                    key=lambda row: (row[0], row[1]),
                )[1]
                direct_job_index = best_stage
            login_job = self.current_login_job if passive_profile else ""
            login_job_index = self.job_name_indexes.get(login_job, 99)
            historical_job = self._historical_profile_job(
                decoded_profile.profile_id
            )
            plate_job, plate_evidence = self._skill_plate_specialization_job(
                direct_job,
                skill_plate_names,
            )
            historical_index = self.job_name_indexes.get(historical_job, 99)
            if login_job and login_job_index < 99:
                decoded_job = login_job
            elif historical_job and historical_index < direct_job_index:
                decoded_job = historical_job
            elif direct_job_index == 1:
                decoded_job = direct_job
            elif plate_job:
                decoded_job = plate_job
            elif direct_job:
                decoded_job = direct_job
            resolved_records.extend(
                inspect_active_set_details(
                    self.inspect_set_catalog,
                    tuple(equipped_set_items),
                )
            )
            equipment = tuple(resolved_records)
            fields += (
                ("正文状态", "XTEA V576 已解密并完成装备档案映射"),
                ("角色档案标识", f"0x{decoded_profile.profile_id:08X}"),
                (
                    "解密参数",
                    f"索引 {decoded_profile.key_index} / {decoded_profile.rounds} 轮 / "
                    f"{decoded_profile.key_source}",
                ),
                ("装备档案映射", f"{mapped_count}/{displayable_count} 条"),
                ("卡片档案", "等待独立 07 7F 登录档案"),
                (
                    "记录分组",
                    " + ".join(str(count) for count in decoded_profile.group_counts),
                ),
                ("全部记录", str(len(decoded_profile.records))),
            )
            fields += tuple(profile_metadata)
            if login_job and decoded_job == login_job:
                fields += ((
                    "职业识别依据",
                    "登录职业码："
                    + "→".join(str(code) for code in self.current_login_job_codes)
                    + f"（{decoded_job}）",
                ),)
            elif historical_job and decoded_job == historical_job:
                fields += (("职业识别依据", f"历史同角色二转技能：{decoded_job}"),)
            elif plate_job and decoded_job == plate_job:
                fields += (
                    (
                        "职业识别依据",
                        f"技能纹章强化分支：{plate_job}（{'、'.join(plate_evidence)}）",
                    ),
                )
            elif direct_job and decoded_job == direct_job:
                fields += (("职业识别依据", f"专属装备/纹章：{decoded_job}"),)
            elif decoded_job:
                fields += (("职业识别依据", f"二转专属装备/纹章：{decoded_job}"),)
            elif direct_job:
                fields += (
                    (
                        "职业识别依据",
                        f"仅确认{direct_job}系，当前档案没有可区分二转的专属证据",
                    ),
                )
            if glyph_groups:
                if active_glyph_group is not None:
                    fields += ((
                        "当前纹章栏",
                        f"第{active_glyph_page_index + 1}栏"
                        f"（档案组 {active_glyph_group}；{glyph_page_source}）",
                    ),)
                else:
                    fields += ((
                        "当前纹章栏",
                        f"未确定（{glyph_page_source}；未应用任何纹章页）",
                    ),)
        else:
            fields += (
                ("正文状态", profile_diagnostic.message),
                ("正文长度", str(profile_diagnostic.body_size)),
                (
                    "密钥选择",
                    f"索引 {profile_diagnostic.key_index} / {profile_diagnostic.rounds} 轮",
                ),
            )
        if strings:
            fields += (("可见文本", " | ".join(strings)),)
        known_actor_ids = set(self.actor_jobs) | set(self.known_actor_names) | set(
            self.skill_actor_ids
        )
        if self_status_actor_id is not None:
            self.local_actor_ids.add(self_status_actor_id)
        request_actor_ids = [
            actor_id
            for actor_id in known_actor_ids
            if actor_id > 0
            and actor_id.to_bytes(4, "little", signed=False) in request_packet.payload
        ]
        if self_status_actor_id is not None:
            request_actor_ids.insert(0, self_status_actor_id)
        elif request_profile_id > 0:
            request_actor_ids.insert(0, request_profile_id)
        non_local_ids = [
            actor_id for actor_id in request_actor_ids if actor_id not in self.local_actor_ids
        ]
        target_actor_id = (
            non_local_ids[0]
            if len(non_local_ids) == 1
            else request_actor_ids[0]
            if len(request_actor_ids) == 1
            else None
        )
        target_name = ""
        target_job = ""
        # Bytes 3..4 stay identical when the same player is inspected again,
        # while the remaining request bytes identify the inspect/close action.
        # Use that session-local target token so equipment changes do not
        # create a second profile merely because the response body changed.
        target_token = (
            f"0x{request_profile_id:08X}"
            if request_profile_id > 0
            else request_packet.payload[3:5].hex().upper()
        )
        target_id = f"inspect:{target_token}"
        if passive_profile and decoded_profile is not None:
            target_id = (
                f"profile:0x{decoded_profile.profile_id:08X}"
                if decoded_profile.profile_id > 0
                else f"self-login:{request_fingerprint.rsplit(':', 1)[-1]}"
            )
            fields += (("响应类型", "被动角色档案"),)
        else:
            fields += (
                (
                    "角色档案标识" if request_profile_id > 0 else "会话角色标识",
                    target_token,
                ),
            )
            if self_status_actor_id is None:
                fields += (("响应类型", "查看信息"),)
        if self_status_actor_id is not None:
            target_actor_id = self_status_actor_id
            target_id = f"0x{self_status_actor_id:08X}"
            target_name, target_job = self._actor_display_context(
                self_status_actor_id
            )
            fields += (
                ("响应类型", "自身状态栏"),
                ("实时ID", target_id),
            )
        elif target_actor_id is not None:
            target_id = f"0x{target_actor_id:08X}"
            target_name, target_job = self._actor_display_context(target_actor_id)
        elif request_profile_id > 0:
            target_id = f"profile:0x{request_profile_id:08X}"
        elif decoded_profile is not None and decoded_profile.profile_id > 0:
            target_id = f"profile:0x{decoded_profile.profile_id:08X}"
        if not target_job:
            target_job = decoded_job
        if not target_job:
            jobs = sorted(set(self.profile_jobs.values()), key=len, reverse=True)
            target_job = next(
                (job for job in jobs if any(job and job in text for text in strings)),
                "",
            )
        origin = self.first_timestamp or packet.timestamp_seconds
        status = (
            "已被动捕获 TCP 角色档案并映射 v576 实际物品"
            if passive_profile and decoded_profile is not None
            else "已解密 TCP 详情响应并映射 v576 实际物品"
            if decoded_profile is not None
            else f"已捕获自身状态栏响应；{profile_diagnostic.message}"
            if self_status_actor_id is not None
            else "已捕获 TCP 详情响应，正文待会话解码"
        )
        event = PlayerInspectEvent(
            timestamp_ms=max(0, round((packet.timestamp_seconds - origin) * 1000)),
            target_id=target_id,
            target_name=target_name,
            job=target_job,
            status=status,
            inspection_key=inspection_key if not passive_profile else "",
            request_fingerprint=request_fingerprint,
            response_layout=response_fingerprint,
            fields=fields,
            equipment=equipment,
            raw_payload_hex=response_packet.payload.hex(),
        )
        if passive_profile and decoded_profile is not None and decoded_profile.profile_id <= 0:
            self.self_login_profile_packet = response_packet
            if self.pending_self_item_archive is not None:
                event = self._merge_inspect_supplement(
                    event, self.pending_self_item_archive
                )
                self.pending_self_item_archive = None
            self.pending_self_profile = event
            self.latest_self_profile = event
            # Compact login profiles do not carry the runtime actor ID.  They
            # are nevertheless the complete local-character archive sent when
            # entering the game, and profile sync deliberately starts from a
            # clean character-select capture session.  V4.24 kept this event
            # private until a self-status request arrived, contradicting the
            # documented "enter the character and read automatically" flow.
            # Publish it under its stable self-login identity now; a later
            # self-status request will still relink the cached event to the
            # authoritative actor ID.
            return event
        return event
    def _resolve_authoritative_glyph_group(
        self,
        glyph_groups: list[int],
        *,
        passive_profile: bool,
        decoded_profile_id: int,
        request_profile_id: int,
        self_status_actor_id: int | None,
    ) -> tuple[int | None, int | None, str]:
        """Select a heraldry group only from authoritative server state.

        ``03 0A`` is merely a client request.  The saved ``07 03`` archive
        contains both pages and carries no active-page marker.  ``03 1D`` is
        therefore the only source allowed to select ``glyph_groups[index]``.
        """
        if not glyph_groups:
            return None, None, "档案未包含纹章页"

        page_index: int | None = None
        actor_candidates: list[int] = []
        for actor_id in (
            self_status_actor_id,
            request_profile_id,
            decoded_profile_id,
        ):
            if actor_id is not None and actor_id > 0 and actor_id not in actor_candidates:
                actor_candidates.append(actor_id)
        for actor_id in actor_candidates:
            if actor_id in self.active_glyph_page_by_actor:
                page_index = self.active_glyph_page_by_actor[actor_id]
                break

        is_login_archive = passive_profile and decoded_profile_id <= 0
        if page_index is None and is_login_archive:
            page_index = self.current_login_glyph_page

        if page_index is None:
            if len(glyph_groups) == 1:
                return glyph_groups[0], 0, "档案仅包含一个纹章页"
            return None, None, "等待服务器 03 1D 确认"
        if not 0 <= page_index < len(glyph_groups):
            return (
                None,
                page_index,
                f"服务器 03 1D 页索引 {page_index} 超出档案范围",
            )
        return glyph_groups[page_index], page_index, "服务器 03 1D 确认"
    @staticmethod
    def _glyph_refresh_supplement(
        previous: PlayerInspectEvent,
    ) -> PlayerInspectEvent:
        """Keep independent login archives while switching glyph pages.

        Re-rendering the immutable 07 03 equipment archive updates the active
        glyph group.  Card mastery (0D 19), card inventory and engravings are
        independent messages, so they must survive that refresh.  Other
        profile fields are deliberately excluded because they may contain the
        old page selection and must come from the freshly rendered archive.
        """
        fields = tuple(
            row
            for row in previous.fields
            if row[0] in {"卡片档案", "刻印档案", "卡片掌握档案"}
            or row[0].startswith("卡片掌握|")
            or row[0].startswith("卡片掌握未解码|")
        )
        equipment = tuple(
            row
            for row in previous.equipment
            if row[0].startswith("刻印|")
        )
        return replace(
            previous,
            fields=fields,
            equipment=equipment,
            raw_payload_hex="",
        )

    @staticmethod
    def _merge_inspect_supplement(
        base: PlayerInspectEvent,
        supplement: PlayerInspectEvent,
    ) -> PlayerInspectEvent:
        """Merge login-time archives without dropping the compact profile."""
        # Field labels are unique in the UI. Later archives replace the
        # earlier "waiting" state for the same label instead of leaving two
        # contradictory rows in the profile.
        fields_by_label: dict[str, str] = {}
        for label, value in base.fields + supplement.fields:
            fields_by_label[label] = value
        fields = tuple(fields_by_label.items())
        equipment_by_key = dict(base.equipment)
        equipment_by_key.update(dict(supplement.equipment))
        cards = supplement.cards or base.cards
        return replace(
            base,
            timestamp_ms=max(base.timestamp_ms, supplement.timestamp_ms),
            target_name=base.target_name or supplement.target_name,
            job=base.job or supplement.job,
            status="已解密本人装备、刻印与卡片档案",
            fields=fields,
            equipment=tuple(equipment_by_key.items()),
            cards=cards,
            raw_payload_hex=base.raw_payload_hex or supplement.raw_payload_hex,
        )
    def _engraving_extra_resolution(self, record):
        """Mirror ``CDnImprintEquip::BuildState`` for the fixed option.

        The client resolves ``itemtable_imprintequip._TypeParam1`` to a
        ``potencetable`` group and uses record byte 29 as a one-based row
        selector.  Byte 30 is not read by BuildState.  Tooltip observations
        are regression fixtures only and never participate in this lookup.
        """
        item = self.inspect_item_catalog.get(record.template_id)
        if item is None or item.category != "刻印":
            return False, None
        if record.field_29 <= 0:
            return True, None
        key = (item.engraving_potence_id, record.field_29)
        options = self.inspect_engraving_potence_catalog
        return key in options, options.get(key)
    def _engraving_extra_for_record(self, record):
        """Compatibility helper returning the resolved attribute only."""
        return self._engraving_extra_resolution(record)[1]
    def _potential_resolution(self, item, record):
        """Resolve TypeParam2 exactly; unknown selectors never wrap around."""
        if item is None or item.potential_id <= 0:
            return None, (), 0
        selected = record.equipment_potential_index
        if selected <= 0:
            return True, (), selected
        key = (item.potential_id, selected)
        catalog = self.inspect_potential_catalog
        return key in catalog, catalog.get(key, ()), selected

    def _potential_attributes_for_record(self, item, record):
        """Compatibility helper returning only exact TypeParam2 attributes."""
        return self._potential_resolution(item, record)[1]

    def _type_param1_resolution(self, item, record, category):
        """Mirror the client's TypeParam1 -> potentialtable row lookup."""
        if (
            item is None
            or category != "装备"
            or item.inherent_potential_id <= 0
        ):
            return None, (), 0
        selected = record.field_29
        if selected <= 0:
            return True, (), selected
        key = (item.inherent_potential_id, selected)
        catalog = self.inspect_inherent_potential_catalog
        return key in catalog, catalog.get(key, ()), selected

    def _type_param3_resolution(self, item, record, category):
        """Resolve the signed, one-based TypeParam3 selector from record[50]."""
        if item is None or category != "装备" or item.type_param3_id <= 0:
            return None, "", (), 0
        selected = record.type_param3_selector
        if selected <= 0:
            return True, "", (), selected
        key = (item.type_param3_id, selected)
        catalog = self.inspect_suffix_potential_catalog
        value = catalog.get(key)
        if value is None:
            return False, "", (), selected
        name, attributes = value
        return True, name, attributes, selected
    @staticmethod
    @lru_cache(maxsize=4)
    def _card_display_catalog(
        data_root: str,
    ) -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, str]]:
        tables = Path(data_root) / "modular" / "tables"
        try:
            lookup_payload = json.loads(
                (tables / "card_lookup.json").read_text(encoding="utf-8")
            )
            levels_payload = json.loads(
                (tables / "card_levels.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}, {}
        lookup = lookup_payload if isinstance(lookup_payload, dict) else {}
        levels: dict[str, str] = {}
        if isinstance(levels_payload, dict):
            for raw_level, raw_names in levels_payload.items():
                if not isinstance(raw_names, list):
                    continue
                for raw_name in raw_names:
                    name = str(raw_name or "").strip()
                    if name:
                        levels[name] = str(raw_level)
        return lookup, levels
    def _card_display_values(
        self, template_id: int
    ) -> tuple[str, str, str, str] | None:
        item = self.inspect_item_catalog.get(template_id)
        if item is None or (
            "卡片" not in item.name and "卡片" not in item.description
        ):
            return None
        card_name = item.name.removeprefix("怪物卡片-").strip()
        lookup, levels = self._card_display_catalog(
            str(Path(__file__).resolve().parents[1] / "data")
        )
        quality = "待确认"
        effects: dict[str, float] = {}
        expected_values = sorted(
            round(float(maximum), 6)
            for _attribute, _minimum, maximum in item.attributes
            if abs(float(maximum)) > 1e-7
        )
        raw_qualities = lookup.get(card_name, {})
        if isinstance(raw_qualities, dict):
            for raw_quality, raw_effects in raw_qualities.items():
                if not isinstance(raw_effects, dict) or not raw_effects:
                    continue
                values = sorted(
                    round(float(value), 6)
                    for value in raw_effects.values()
                )
                if values == expected_values:
                    quality = str(raw_quality)
                    effects = {
                        str(name): float(value)
                        for name, value in raw_effects.items()
                    }
                    break
        effect_text = "、".join(
            f"{name}+{value:g}" for name, value in effects.items()
        ) or "待确认"
        return card_name, quality, levels.get(card_name, "待确认"), effect_text
    def _observe_self_item_archive(
        self,
        packet: PacketRecord,
    ) -> PlayerInspectEvent | None:
        """Publish complete login-time card and engraving archives."""
        if (
            packet.transport.upper() != "TCP"
            or packet.inferred_direction() != "server_to_client"
        ):
            return None
        for payload in _split_length_framed_tcp_messages(packet.payload):
            decoded = decode_tcp_item_archive_payload(payload)
            if decoded is None:
                continue
            cards: list[tuple[str, str, str, str, str, str]] = []
            equipment: list[tuple[str, str]] = []
            if decoded.archive_kind == "cards":
                for archive_position, record in enumerate(decoded.records):
                    display = self._card_display_values(record.template_id)
                    if display is None:
                        continue
                    card_name, quality, level, effect_text = display
                    page = archive_position // 16 + 1
                    position = archive_position % 16 + 1
                    cards.append(
                        (
                            f"第{page}页-{position}",
                            card_name,
                            quality,
                            level,
                            str(record.reseal_count),
                            effect_text,
                        )
                    )
                if len(cards) != len(decoded.records):
                    continue
            else:
                engraving_slots: Counter[str] = Counter()
                for record in decoded.records:
                    item = self.inspect_item_catalog.get(record.template_id)
                    category = inspect_record_category(3, item)
                    if category != "刻印":
                        continue
                    slot = inspect_record_slot(
                        3, record.record_index, category, item
                    )
                    engraving_slots[slot] += 1
                    if slot == "戒指":
                        slot = f"戒指{engraving_slots[slot]}"
                    extra_resolved, extra = self._engraving_extra_resolution(
                        record
                    )
                    equipment.append(
                        (
                            f"{category}|{slot}",
                            inspect_record_detail(
                                record.template_id,
                                record.enhance_level,
                                item,
                                dynamic_attributes=record.dynamic_attributes,
                                engraving_extra_attributes=(
                                    (extra[0],) if extra is not None else ()
                                ),
                                engraving_extra_resolved=extra_resolved,
                                engraving_extra_selector=(
                                    record.engraving_extra_selector
                                ),
                            ),
                        )
                    )
                if len(equipment) != len(decoded.records):
                    continue

            actor_id = self.current_self_actor_id
            if actor_id is None and len(self.local_actor_ids) == 1:
                actor_id = next(iter(self.local_actor_ids))
            target_name, target_job = (
                self._actor_display_context(actor_id)
                if actor_id is not None
                else (self.local_name, "")
            )
            origin = self.first_timestamp or packet.timestamp_seconds
            event = PlayerInspectEvent(
                timestamp_ms=max(
                    0, round((packet.timestamp_seconds - origin) * 1000)
                ),
                target_id=(
                    f"0x{actor_id:08X}"
                    if actor_id is not None
                    else "self-login:archives"
                ),
                target_name=target_name,
                job=target_job,
                status=(
                    f"已解密完整卡片档案（{len(cards)}张）"
                    if cards
                    else f"已解密完整刻印档案（{len(equipment)}件）"
                ),
                response_layout=(
                    f"TCP:{decoded.archive_kind}_archive:"
                    f"0x{decoded.opcode[1]:02X}:{len(decoded.records)}"
                ),
                fields=(
                    (
                        "档案类型",
                        "卡片" if cards else "刻印",
                    ),
                    (
                        "解密参数",
                        f"索引 {decoded.key_index} / {decoded.rounds} 轮 / "
                        f"{decoded.key_source}",
                    ),
                    ("档案记录", str(len(decoded.records))),
                    (
                        "卡片档案" if cards else "刻印档案",
                        f"已解密 {len(cards) if cards else len(equipment)} 条",
                    ),
                ),
                equipment=tuple(equipment),
                cards=tuple(cards),
                raw_payload_hex=payload.hex(),
            )
            if self.latest_self_profile is not None:
                self.latest_self_profile = self._merge_inspect_supplement(
                    self.latest_self_profile, event
                )
                if actor_id is not None:
                    self.latest_self_profile = replace(
                        self.latest_self_profile,
                        target_id=f"0x{actor_id:08X}",
                        target_name=(
                            target_name or self.latest_self_profile.target_name
                        ),
                        job=target_job or self.latest_self_profile.job,
                    )
                self.pending_self_profile = self.latest_self_profile
                # Keep the already-published compact login profile current so
                # profile sync can include card/engraving archives without
                # requiring the player to open the local status panel.
                return self.latest_self_profile
            if actor_id is not None:
                return event
            if self.pending_self_profile is not None:
                self.pending_self_profile = self._merge_inspect_supplement(
                    self.pending_self_profile, event
                )
                self.latest_self_profile = self.pending_self_profile
                return self.pending_self_profile
            elif self.pending_self_item_archive is not None:
                self.pending_self_item_archive = self._merge_inspect_supplement(
                    self.pending_self_item_archive, event
                )
            else:
                self.pending_self_item_archive = event
            return None
        return None
    def _observe_self_card_mastery(
        self,
        packet: PacketRecord,
    ) -> PlayerInspectEvent | None:
        """Publish the authoritative S2C 0D 19 card-mastery level array."""
        if (
            packet.transport.upper() != "TCP"
            or packet.inferred_direction() != "server_to_client"
        ):
            return None
        for payload in _split_length_framed_tcp_messages(packet.payload):
            decoded = decode_tcp_card_mastery_payload(
                payload,
                connection_context=packet,
            )
            if decoded is None:
                continue
            observed = dict(decoded.entries)
            visible_rows = sorted(
                (
                    (tree_slot, mastery_id, name)
                    for mastery_id, (
                        name,
                        tree_slot,
                        _state_type,
                        hidden,
                    ) in self.inspect_card_mastery_catalog.items()
                    if not hidden
                ),
                key=lambda row: (row[0], row[1]),
            )
            levels = tuple(
                (name, observed.get(mastery_id, 0))
                for _tree_slot, mastery_id, name in visible_rows
            )
            unknown_entries = tuple(
                (mastery_id, level)
                for mastery_id, level in decoded.entries
                if mastery_id not in self.inspect_card_mastery_catalog
            )
            if not levels:
                continue
            actor_id = self.current_self_actor_id
            if actor_id is None and len(self.local_actor_ids) == 1:
                actor_id = next(iter(self.local_actor_ids))
            target_name, target_job = (
                self._actor_display_context(actor_id)
                if actor_id is not None
                else (self.local_name, "")
            )
            origin = self.first_timestamp or packet.timestamp_seconds
            event = PlayerInspectEvent(
                timestamp_ms=max(
                    0, round((packet.timestamp_seconds - origin) * 1000)
                ),
                target_id=(
                    f"0x{actor_id:08X}"
                    if actor_id is not None
                    else "self-login:card-mastery"
                ),
                target_name=target_name,
                job=target_job,
                status=(
                    f"已识别卡片掌握之力（{len(levels)}项）"
                    if not unknown_entries
                    else (
                        f"已识别卡片掌握之力（{len(levels)}项）；"
                        f"仍有 {len(unknown_entries)} 项未解码"
                    )
                ),
                response_layout="TCP:card_mastery_archive:0D19",
                fields=tuple(
                    (f"卡片掌握|{name}", str(level))
                    for name, level in levels
                )
                + tuple(
                    (
                        f"卡片掌握未解码|类型 {mastery_id}",
                        f"未解码（等级 {level}）",
                    )
                    for mastery_id, level in unknown_entries
                )
                + (("卡片掌握档案", "服务器 0D 19 逐项解码"),),
            )
            if self.latest_self_profile is not None:
                self.latest_self_profile = self._merge_inspect_supplement(
                    self.latest_self_profile, event
                )
                if actor_id is not None:
                    self.latest_self_profile = replace(
                        self.latest_self_profile,
                        target_id=f"0x{actor_id:08X}",
                        target_name=(
                            target_name or self.latest_self_profile.target_name
                        ),
                        job=target_job or self.latest_self_profile.job,
                    )
                self.pending_self_profile = self.latest_self_profile
                return self.latest_self_profile
            if actor_id is not None:
                return event
            if self.pending_self_profile is not None:
                self.pending_self_profile = self._merge_inspect_supplement(
                    self.pending_self_profile, event
                )
                self.latest_self_profile = self.pending_self_profile
                return self.pending_self_profile
            elif self.pending_self_item_archive is not None:
                self.pending_self_item_archive = self._merge_inspect_supplement(
                    self.pending_self_item_archive, event
                )
            else:
                self.pending_self_item_archive = event
            return None
        return None
    def _observe_card_profile_update(
        self,
        packet: PacketRecord,
    ) -> PlayerInspectEvent | None:
        """Publish a verified card-page change without treating it as a full archive."""
        if (
            packet.transport.upper() != "TCP"
            or packet.inferred_direction() != "server_to_client"
        ):
            return None
        for payload in _split_length_framed_tcp_messages(packet.payload):
            decoded = decode_tcp_card_update_payload(payload)
            if decoded is None:
                continue
            item = self.inspect_item_catalog.get(decoded.template_id)
            if item is None:
                continue
            card_name = item.name.removeprefix("怪物卡片-").strip()
            if "卡片" not in item.name and "卡片" not in item.description:
                continue
            lookup, levels = self._card_display_catalog(
                str(Path(__file__).resolve().parents[1] / "data")
            )
            quality = "待确认"
            effects: dict[str, float] = {}
            expected_values = sorted(
                round(float(maximum), 6)
                for _attribute, _minimum, maximum in item.attributes
                if abs(float(maximum)) > 1e-7
            )
            raw_qualities = lookup.get(card_name, {})
            if isinstance(raw_qualities, dict):
                for raw_quality, raw_effects in raw_qualities.items():
                    if not isinstance(raw_effects, dict) or not raw_effects:
                        continue
                    values = sorted(
                        round(float(value), 6)
                        for value in raw_effects.values()
                    )
                    if values == expected_values:
                        quality = str(raw_quality)
                        effects = {
                            str(name): float(value)
                            for name, value in raw_effects.items()
                        }
                        break
            effect_text = "、".join(
                f"{name}+{value:g}" for name, value in effects.items()
            ) or "待确认"
            actor_id = self.current_self_actor_id
            if actor_id is None and len(self.local_actor_ids) == 1:
                actor_id = next(iter(self.local_actor_ids))
            target_id = (
                f"0x{actor_id:08X}"
                if actor_id is not None
                else f"self-card:{decoded.instance_token[:16]}"
            )
            target_name, target_job = (
                self._actor_display_context(actor_id)
                if actor_id is not None
                else (self.local_name, "")
            )
            action = "装备" if decoded.equipped else "卸下"
            origin = self.first_timestamp or packet.timestamp_seconds
            return PlayerInspectEvent(
                timestamp_ms=max(
                    0, round((packet.timestamp_seconds - origin) * 1000)
                ),
                target_id=target_id,
                target_name=target_name,
                job=target_job,
                status=f"已识别卡片{action}变更；完整卡片档案仍在解密",
                response_layout=(
                    f"TCP:card_update:0x{decoded.action_code:02X}:"
                    f"{decoded.record_offset}"
                ),
                fields=(
                    ("卡片变更", f"{action} {card_name}"),
                    (
                        "解密参数",
                        f"索引 {decoded.key_index} / {decoded.rounds} 轮 / "
                        f"{decoded.key_source}",
                    ),
                    ("卡片档案", "已确认单张变更；未冒充完整卡片页"),
                ),
                cards=(
                    (
                        f"本次{action}",
                        card_name,
                        quality,
                        levels.get(card_name, "待确认"),
                        "待确认",
                        effect_text,
                    ),
                ),
                raw_payload_hex=payload.hex(),
            )
        return None


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'bind_host_globals', 'V576ProfileDecoderMixin'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value
    # These unchanged methods originally lived in ``live.sources`` and
    # resolve the package data directory relative to that source file.
    # Preserve the original ``__file__`` anchor after moving the methods.
    source_file = host_globals.get('__file__')
    if isinstance(source_file, str):
        globals()['__file__'] = source_file


# Extracted from sources.py; algorithm bodies are unchanged.
