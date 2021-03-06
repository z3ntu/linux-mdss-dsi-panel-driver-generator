# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import Enum, unique
from typing import Iterator, List, Optional

import libfdt

import mipi
from fdt2 import Fdt2


@unique
class Mode(Enum):
	VIDEO_MODE = 'dsi_video_mode', ['MIPI_DSI_MODE_VIDEO']
	CMD_MODE = 'dsi_cmd_mode', []

	def __new__(cls, value: str, flags: List[str]) -> Mode:
		obj = object.__new__(cls)
		obj._value_ = value
		obj.flags = flags
		return obj


@unique
class TrafficMode(Enum):
	SYNC_PULSE = 'non_burst_sync_pulse', ['MIPI_DSI_MODE_VIDEO_SYNC_PULSE']
	SYNC_EVENT = 'non_burst_sync_event', []
	BURST_MODE = 'burst_mode', ['MIPI_DSI_MODE_VIDEO_BURST']

	def __new__(cls, value: str, flags: List[str]) -> TrafficMode:
		obj = object.__new__(cls)
		obj._value_ = value
		obj.flags = flags
		return obj

	@staticmethod
	def parse(prop: libfdt.Property) -> Optional[TrafficMode]:
		if prop[-1] == 0:  # Null terminated string
			return TrafficMode(prop.as_str())

		print(f"WARNING: qcom,mdss-dsi-traffic-mode is not a null terminated string:", prop)

		# Some Samsung panels have the traffic mode as index for some reason
		if len(prop) == 4:
			i = prop.as_uint32()
			traffic_modes = list(TrafficMode.__members__.values())
			if i < len(traffic_modes):
				print(f"Interpreting qcom,mdss-dsi-traffic-mode as numeric index: {i} == {traffic_modes[i]}")
				return traffic_modes[i]

		# Use the default in mdss_dsi_panel.c
		print("Falling back to MIPI_DSI_MODE_VIDEO_SYNC_PULSE")
		return TrafficMode.SYNC_PULSE

@unique
class BacklightControl(Enum):
	PWM = 'bl_ctrl_pwm'
	DCS = 'bl_ctrl_dcs'
	WLED = 'bl_ctrl_wled'


class Dimension:
	@unique
	class Type(Enum):
		HORIZONTAL = 'h', 'width'
		VERTICAL = 'v', 'height'

		def __init__(self, prefix: str, size: str) -> None:
			self.prefix = prefix
			self.size = size

	def __init__(self, fdt: Fdt2, node: int, t: Type) -> None:
		self.type = type
		self.px = fdt.getprop(node, f'qcom,mdss-dsi-panel-{t.size}').as_int32()
		self.fp = fdt.getprop(node, f'qcom,mdss-dsi-{t.prefix}-front-porch').as_int32()
		self.bp = fdt.getprop(node, f'qcom,mdss-dsi-{t.prefix}-back-porch').as_int32()
		self.pw = fdt.getprop(node, f'qcom,mdss-dsi-{t.prefix}-pulse-width').as_int32()
		self.size = fdt.getprop_int32(node, f'qcom,mdss-pan-physical-{t.size}-dimension')


@dataclass
class Command:
	type: mipi.Transaction
	last: bool
	vc: int
	ack: bool
	wait: int
	payload: bytes
	generated: str = None


class CommandSequence:
	generated: str = ''

	@unique
	class State(Enum):
		LP_MODE = 'dsi_lp_mode'
		HS_MODE = 'dsi_hs_mode'

	def __init__(self, fdt: Fdt2, node: int, cmd: str) -> None:
		self.state = CommandSequence.State(fdt.getprop(node, f'qcom,mdss-dsi-{cmd}-command-state').as_str())
		self.seq = []

		prop = fdt.getprop_or_none(node, f'qcom,mdss-dsi-{cmd}-command')
		if prop is None:
			print(f'Warning: qcom,mdss-dsi-{cmd}-command does not exist')
			return  # No commands
		itr = iter(prop)

		if cmd == 'on':
			# WHY SONY, WHY?????? Just put it in on-command...
			init = fdt.getprop_or_none(node, 'somc,mdss-dsi-init-command')
			if init:
				itr = itertools.chain(init, itr)

		for dtype in itr:
			last, vc, ack, wait = next(itr), next(itr), next(itr), next(itr)
			dlen = next(itr) << 8 | next(itr)
			payload = bytes(next(itr) for _ in range(0, dlen))

			t = mipi.Transaction(dtype)

			# Very often there are too many arguments encoded in the command stream.
			# These are redundant, because they would be never sent anyway.
			max_dlen = t.max_args + 1
			if 0 < max_dlen < dlen:
				payload = payload[:max_dlen]

			self.seq.append(Command(t, last, vc, ack, wait, payload))


def _remove_prefixes(text: str, *args: str) -> str:
	for prefix in args:
		text = text[len(prefix):] if text.startswith(prefix) else text
	return text


def _replace_all(text: str, *args: str) -> str:
	for replace in args:
		text = text.replace(replace, '')
	return text


def _remove_before(text: str, sub: str) -> str:
	i = text.find(sub)
	return text[i + 1:] if i >= 0 else text


class Panel:
	def __init__(self, name: str, fdt: Fdt2, node: int) -> None:
		self.name = name
		self.id = _remove_before(_remove_prefixes(fdt.get_name(node), 'qcom,mdss_dsi_', 'ss_dsi_panel_', 'mot_').lower(), ',')
		print(f'Parsing: {self.id} ({name})')
		self.short_id = _replace_all(self.id, '_panel', '_video', '_vid', '_cmd',
									 '_hd', '_qhd', '_720p', '_1080p',
									 '_wvga', '_fwvga', '_qvga', '_xga', '_wxga')
		self.h = Dimension(fdt, node, Dimension.Type.HORIZONTAL)
		self.v = Dimension(fdt, node, Dimension.Type.VERTICAL)
		self.framerate = fdt.getprop(node, 'qcom,mdss-dsi-panel-framerate').as_int32()
		self.bpp = fdt.getprop(node, 'qcom,mdss-dsi-bpp').as_int32()
		self.mode = Mode(fdt.getprop(node, 'qcom,mdss-dsi-panel-type').as_str())
		self.traffic_mode = TrafficMode.parse(fdt.getprop(node, 'qcom,mdss-dsi-traffic-mode'))
		backlight = fdt.getprop_or_none(node, 'qcom,mdss-dsi-bl-pmic-control-type')
		self.backlight = BacklightControl(backlight.as_str()) if backlight else None
		self.max_brightness = fdt.getprop_int32(node, 'qcom,mdss-dsi-bl-max-level', None)

		self.lanes = 0
		while fdt.getprop_or_none(node, f'qcom,mdss-dsi-lane-{self.lanes}-state') is not None:
			self.lanes += 1

		self.flags = self.mode.flags + self.traffic_mode.flags

		if fdt.getprop_int32(node, 'qcom,mdss-dsi-h-sync-pulse') != 0:
			self.flags.append('MIPI_DSI_MODE_VIDEO_HSE')

		if fdt.getprop_or_none(node, 'qcom,mdss-dsi-tx-eot-append') is None:
			self.flags.append('MIPI_DSI_MODE_EOT_PACKET')

		if fdt.getprop_or_none(node, 'qcom,mdss-dsi-force-clock-lane-hs') is None \
				and fdt.getprop_or_none(node, 'qcom,mdss-dsi-force-clk-lane-hs') is None:
			self.flags.append('MIPI_DSI_CLOCK_NON_CONTINUOUS')

		reset_seq = fdt.getprop_or_none(node, 'qcom,mdss-dsi-reset-sequence')
		if reset_seq is not None:
			itr = iter(reset_seq.as_uint32_array())
			self.reset_seq = list(zip(itr, itr))
		else:
			self.reset_seq = None

		self.cmds = {
			'on': CommandSequence(fdt, node, 'on'),
			'off': CommandSequence(fdt, node, 'off')
		}

		# If all commands are sent in LPM, add flag globally
		if self.cmds['on'].state == CommandSequence.State.LP_MODE == self.cmds['off'].state:
			self.flags.append('MIPI_DSI_MODE_LPM')

		if self.bpp == 24:
			self.format = 'MIPI_DSI_FMT_RGB888'
		else:
			raise ValueError(f'Unsupported bpp: {self.bpp} (TODO)')

		# Sony </3
		prop = fdt.getprop_or_none(node, 'somc,mdss-phy-size-mm')
		if prop:
			phy_size_mm = prop.as_uint32_array()
			self.h.size = phy_size_mm[0]
			self.v.size = phy_size_mm[1]

	@staticmethod
	def parse(fdt: Fdt2, node: int) -> Panel:
		name = fdt.getprop_or_none(node, 'qcom,mdss-dsi-panel-name')
		return name and Panel(name.as_str(), fdt, node)

	@staticmethod
	def find(fdt: Fdt2) -> Iterator[Panel]:
		for mdp in fdt.find_by_compatible('qcom,mdss_mdp'):
			for sub in fdt.subnodes(mdp):
				panel = Panel.parse(fdt, sub)
				if panel:
					yield panel
