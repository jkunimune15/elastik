#!/usr/bin/env python
"""
create_map_projection.py

take a basic mesh and optimize it according to a particular cost function in order to
create a new Elastic Projection.
"""
import math
import traceback

import h5py
import numpy as np
import shapefile
import tifffile
from matplotlib import pyplot as plt
from scipy import interpolate

from cmap import CUSTOM_CMAP
from optimize import minimize
from sparse import DenseSparseArray
from util import dilate, h5_str, EARTH, index_grid, Scalar


def get_bounding_box(points: np.ndarray) -> np.ndarray:
	""" compute the maximum and minimums of this set of points and package it as
	    [[left, bottom], [right, top]]
	"""
	return np.array([
		[np.nanmin(points[..., 0]), np.nanmin(points[..., 1])],
		[np.nanmax(points[..., 0]), np.nanmax(points[..., 1])], # TODO: account for the border, and for spline interpolation
	])


def downsample(full: np.ndarray, shape: tuple):
	""" decrease the size of a numpy array by setting each pixel to the mean of the pixels
	    in the original image for which it was the nearest neibor
	"""
	if full.shape == ():
		return np.full(shape, full)
	assert len(shape) == len(full.shape)
	for i in range(len(shape)):
		assert shape[i] < full.shape[i]
	reduc = np.empty(shape)
	i_reduc = (np.arange(full.shape[0])/full.shape[0]*reduc.shape[0]).astype(int)
	j_reduc = (np.arange(full.shape[1])/full.shape[1]*reduc.shape[1]).astype(int)
	for i in range(shape[0]):
		for j in range(shape[1]):
			reduc[i][j] = np.mean(full[i_reduc == i][:, j_reduc == j])
	return reduc


def follow_graph(progression: np.ndarray, frum: np.ndarray, until: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
	""" take with a markov-chain-like-thing in the form of an array of indices and follow
	    it to some conclusion.
	    :param progression: the indices of the nodes that follow from each node
	    :param frum: the starting state, an array of indices
	    :param until: a boolean array indicating points that don't need to follow to
	                        the next part of the graph
	"""
	state = frum.copy()
	distance_traveld = np.zeros(state.shape)
	arrived = until[state]
	while np.any(~arrived):
		if np.any((~arrived) & (progression[state] == state)):
			raise ValueError("this importance graff was about to cause an infinite loop.")
		state[~arrived] = progression[state[~arrived]]
		distance_traveld[~arrived] += 1
		arrived = until[state]
	return state, distance_traveld


def get_interpolation_weits(distance_a, distance_b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
	""" compute the weits needed to linearly interpolate a point between two fixed points,
	    given the distance of each respective reference to the point of interpolation.
	"""
	weits_a = np.empty(distance_a.shape)
	normal = distance_a + distance_b != 0
	weits_a[normal] = distance_b[normal]/(distance_a + distance_b)[normal]
	weits_a[~normal] = 1
	weits_b = 1 - weits_a
	return weits_a, weits_b


def enumerate_nodes(mesh: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
	""" take an array of positions for a mesh and generate the list of unique nodes,
	    returning mappings from the old mesh to the new indices and the n×2 list positions
	"""
	# first flatten and sort the positions
	node_positions = mesh.reshape((-1, mesh.shape[-1]))
	node_positions, node_indices = np.unique(node_positions, axis=0, return_inverse=True)
	node_indices = node_indices.reshape(mesh.shape[:-1])

	# then remove any nans, which in fact represent the absence of a node
	nan_index = np.nonzero(np.isnan(node_positions[:, 0]))[0][0]
	node_positions = node_positions[:nan_index, :]
	node_indices[node_indices >= nan_index] = -1

	return node_indices, node_positions


def enumerate_cells(node_indices: np.ndarray, values: list[np.ndarray], scales: list[np.ndarray],
                    dΦ: np.ndarray, dΛ: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
	""" take an array of nodes and generate the list of cells in which the elastic energy
	    should be calculated.
	    :param node_indices: the lookup table that tells you the index in the position
	                         vector at which is stored each node at each location in the
	                         mesh
	    :param values: the relative importance of each cell in the cell matrix
	    :param scales: the desired relative areal scale factor for each location in the cell matrix
	    :param dΦ: the spacing between each adjacent row of nodes (km)
	    :param dΛ: the spacing between adjacent nodes in each row (km)
	    :return: cell_definitions: the list of cells, each defined by a set of seven
	                               indices (the section index, the two indices specifying
	                               its location the matrix, and the indices of the four
	                               vertex nodes (two of them are probably the same node)
	                               in the node vector
	             cell_weights: the volume of each cell for elastic-energy-summing porpoises
	             cell_scales: the desired relative linear scale factor for each cell

	"""
	# start off by resampling these in a useful way
	for h in range(node_indices.shape[0]):
		values[h] = downsample(values[h], node_indices.shape[1:])
		scales[h] = downsample(scales[h], node_indices.shape[1:])
	values = np.stack(values)
	scales = np.stack(scales)

	# assemble a list of all possible cells
	h, i, j = index_grid((node_indices.shape[0],
	                      node_indices.shape[1] - 1,
	                      node_indices.shape[2] - 1))
	h, i, j = h.ravel(), i.ravel(), j.ravel()
	cell_definitions = np.empty((0, 12), dtype=int)
	for di in range(0, 2):
		for dj in range(0, 2):
			# define them by their indices and neiboring node indices
			west_node = node_indices[h, i + di, j]
			east_node = node_indices[h, i + di, j + 1]
			south_node = node_indices[h, i,     j + dj]
			north_node = node_indices[h, i + 1, j + dj]
			cell_definitions = np.concatenate([
				cell_definitions,
				np.stack([h, i, j, i + di, i + 1 - di, # these first five will get chopd off once I'm done with them
					      h, i + di, j + dj, # these middle three are for generic spacially dependent stuff
				          west_node, east_node, # these bottom four are the really important indices
				          south_node, north_node], axis=-1)])

	# then remove all duplicates
	_, unique_indices = np.unique(cell_definitions[:, -4:], axis=0, return_index=True)
	cell_definitions = cell_definitions[unique_indices, :]

	# and remove the ones that rely on missingnodes or that rely on the poles too many times
	missing_node = np.any(cell_definitions[:, -4:] == -1, axis=1)
	degenerate = cell_definitions[:, -4] == cell_definitions[:, -3]
	cell_definitions = cell_definitions[~(missing_node | degenerate), :]

	# you can pull apart the cell definitions now
	cell_hs = cell_definitions[:, 0]
	cell_is = cell_definitions[:, 1]
	cell_js = cell_definitions[:, 2]
	cell_node1_is = cell_definitions[:, 3]
	cell_node2_is = cell_definitions[:, 4]
	cell_definitions = cell_definitions[:, 5:]

	# finally, calculate their areas and stuff
	A_1 = dΦ[cell_node1_is]*dΛ[cell_node1_is]
	A_2 = dΦ[cell_node2_is]*dΛ[cell_node2_is]
	cell_areas = (3*A_1 + A_2)/16/(4*np.pi*EARTH.R**2)

	cell_weights = cell_areas*values[cell_hs, cell_is, cell_js]
	cell_scales = np.sqrt(scales[cell_hs, cell_is, cell_js]) # this sqrt converts it from areal scale to linear

	return cell_definitions, cell_weights, cell_scales


def mesh_skeleton(lookup_table: np.ndarray, factor: int, ф: np.ndarray
                  ) -> tuple[np.ndarray | DenseSparseArray | Scalar, np.ndarray | DenseSparseArray | Scalar]:
	""" create a pair of inverse functions that transform points between the full space of
	    possible meshes and a reduced space with fewer degrees of freedom. the idea here
	    is to identify 80% or so of the nodes that can form a skeleton, and from which the
	    remaining nodes can be interpolated. to that end, each parallel will have some
	    number of regularly spaced key nodes, and all nodes adjacent to an edge will
	    be keys, as well, and all nodes that aren't key nodes will be ignored when
	    reducing the position vector and interpolated from neibors when restoring it.
	    :param lookup_table: the index of each node's position in the state vector
	    :param factor: approximately how much the resolution should decrease
	    :param ф: the latitudes of the nodes
	    :return: a matrix that linearly reduces a set of node positions to just the bare
	             skeleton, and a matrix that linearly reconstructs the missing node
	             positions from a reduced set.  don't worry about their exact types;
	             they'll both support matrix multiplication with '@'.
	"""
	n_full = np.max(lookup_table) + 1
	# start by filling out these connection graffs, which are nontrivial because of the layers
	east_neibor = np.full(n_full, -1)
	west_neibor = np.full(n_full, -1)
	north_neibor = np.full(n_full, -1)
	south_neibor = np.full(n_full, -1)
	for h in range(lookup_table.shape[0]):
		for i in range(lookup_table.shape[1]):
			for j in range(lookup_table.shape[2]):
				if lookup_table[h, i, j] != -1:
					if j - 1 >= 0 and lookup_table[h, i, j - 1] != -1:
						west_neibor[lookup_table[h, i, j]] = lookup_table[h, i, j - 1]
					if j + 1 < lookup_table.shape[2] and lookup_table[h, i, j + 1] != -1:
						east_neibor[lookup_table[h, i, j]] = lookup_table[h, i, j + 1]
					if i - 1 >= 0 and lookup_table[h, i - 1, j] != -1:
						south_neibor[lookup_table[h, i, j]] = lookup_table[h, i - 1, j]
					if i + 1 < lookup_table.shape[1] and lookup_table[h, i + 1, j] != -1:
						north_neibor[lookup_table[h, i, j]] = lookup_table[h, i + 1, j]

	# then decide which nodes should be independently defined in the skeleton
	has_defined_neibors = np.full(n_full + 1, False) # (this array has an extra False at the end so that -1 works nicely)
	is_defined = np.full(n_full, False)
	# start by marking some evenly spaced interior points
	for h in range(lookup_table.shape[0]):
		for i in range(lookup_table.shape[1]):
			east_west_factor = int(round(factor/np.cos(ф[i])))
			for j in range(lookup_table.shape[2]):
				if lookup_table[h, i, j] != -1:
					important_row = (min(i, ф.size - 1 - i)%factor == 0)
					important_col = (j%east_west_factor == 0)
					has_defined_neibors[lookup_table[h, i, j]] |= important_row
					is_defined[lookup_table[h, i, j]] |= important_col
	# then make sure we define enuff points at each edge to keep it all fully defined
	has_defined_neibors[:-1] |= (north_neibor == -1) | (south_neibor == -1)
	is_defined |= (~has_defined_neibors[east_neibor]) | (~has_defined_neibors[west_neibor])
	is_defined &= has_defined_neibors[:-1]

	reindex = np.where(is_defined, np.cumsum(is_defined) - 1, -1)
	n_partial = np.max(reindex) + 1

	# then decide how to define the ones that aren't defined
	n_reference, n_distance = follow_graph(north_neibor, frum=np.arange(n_full), until=has_defined_neibors)
	ne_reference, ne_distance = follow_graph(east_neibor, frum=n_reference, until=is_defined)
	nw_reference, nw_distance = follow_graph(west_neibor, frum=n_reference, until=is_defined)
	s_reference, s_distance = follow_graph(south_neibor, frum=np.arange(n_full), until=has_defined_neibors)
	se_reference, se_distance = follow_graph(east_neibor, frum=s_reference, until=is_defined)
	sw_reference, sw_distance = follow_graph(west_neibor, frum=s_reference, until=is_defined)
	n_weit, s_weit = get_interpolation_weits(n_distance, s_distance)
	ne_weit, nw_weit = get_interpolation_weits(ne_distance, nw_distance)
	se_weit, sw_weit = get_interpolation_weits(se_distance, sw_distance)
	defining_indices = np.stack([
		ne_reference, nw_reference, se_reference, sw_reference,
	], axis=1)
	defining_weits = np.stack([
		n_weit * ne_weit,
		n_weit * nw_weit,
		s_weit * se_weit,
		s_weit * sw_weit,
	], axis=1)

	# put the conversions together and return them as functions
	reduction = DenseSparseArray.identity(n_full)[is_defined, :]
	restoration = DenseSparseArray.from_coordinates(
		[n_partial], np.expand_dims(reindex[defining_indices], axis=-1), defining_weits)
	return reduction, restoration


def compute_principal_strains(positions: np.ndarray,
                              cell_definitions: np.ndarray, cell_scales: np.ndarray,
                              dΦ: np.ndarray, dΛ: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
	""" take a set of cell definitions and 2D coordinates for each node, and calculate
	    the Tissot-ellipse semiaxes of each cell.
	    :param positions: the vector specifying the location of each node in the map plane
	    :param cell_definitions: the list of cells, each defined by seven indices
	    :param cell_scales: the linear scale factor for each cell
	    :param dΦ: the distance between adjacent rows of nodes (km)
	    :param dΛ: the distance between adjacent nodes in each row (km)
	"""
	i = cell_definitions[:, 1]

	west = positions[cell_definitions[:, 3], :]
	east = positions[cell_definitions[:, 4], :]
	F_λ = ((east - west)/(dΛ[i]/cell_scales)[:, np.newaxis])
	dxdΛ, dydΛ = F_λ[:, 0], F_λ[:, 1]

	south = positions[cell_definitions[:, 5], :]
	north = positions[cell_definitions[:, 6], :]
	F_ф = ((north - south)/(dΦ[i]/cell_scales)[:, np.newaxis])
	dxdΦ, dydΦ = F_ф[:, 0], F_ф[:, 1]

	trace = np.sqrt((dxdΛ + dydΦ)**2 + (dxdΦ - dydΛ)**2)/2
	antitrace = np.sqrt((dxdΛ - dydΦ)**2 + (dxdΦ + dydΛ)**2)/2
	return trace + antitrace, trace - antitrace


def load_options(filename: str) -> dict[str, str]:
	""" load a simple colon-separated text file """
	options = dict()
	with open(f"../spec/options_{filename}.txt", "r", encoding="utf-8") as file:
		for line in file.readlines():
			key, value = line.split(":")
			options[key.strip()] = value.strip()
	return options


def load_pixel_values(filename: str, cut_set: str, num_sections: int, minimum=-inf) -> list[np.ndarray]:
	""" load and resample a generic 2D raster image """
	if filename == "uniform":
		return [np.array(1.)]*num_sections
	else:
		values = []
		for h in range(num_sections):
			values.append(np.maximum(
				minimum, tifffile.imread(f"../spec/pixels_{cut_set}_{h}_{filename}.tif")))
		return values


def load_coastline_data(reduction=2) -> list[np.ndarray]:
	coastlines = []
	with shapefile.Reader(f"../data/ne_110m_coastline.zip") as shapef:
		for shape in shapef.shapes():
			if len(shape.points) > 3*reduction:
				coastlines.append(np.radians(shape.points)[::reduction, ::-1])
	return coastlines


def load_mesh(filename: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[np.ndarray]]:
	""" load the ф values, λ values, node locations, and section borders from a HDF5
	    file, in that order.
	"""
	with h5py.File(f"../spec/mesh_{filename}.h5", "r") as file:
		ф = np.radians(file["section0/latitude"])
		λ = np.radians(file["section0/longitude"])
		num_sections = file.attrs["num_sections"]
		mesh = np.empty((num_sections, ф.size, λ.size, 2))
		sections = []
		for h in range(num_sections):
			mesh[h, :, :, :] = file[f"section{h}/projection"]
			sections.append(np.radians(file[f"section{h}/border"][:, :]))
	return ф, λ, mesh, sections


def show_mesh(fit_positions: np.ndarray, all_positions: np.ndarray, velocity: np.ndarray,
              values: list[float], grads: list[float], final: bool,
              ф_mesh: np.ndarray, λ_mesh: np.ndarray, dΦ: np.ndarray, dΛ: np.ndarray,
              mesh_index: np.ndarray, cell_definitions: np.ndarray,
              cell_weights: np.ndarray, cell_scales: np.ndarray,
              coastlines: list[np.array],
              map_axes: plt.Axes, hist_axes: plt.Axes,
              valu_axes: plt.Axes, diff_axes: plt.Axes) -> None:
	map_axes.clear()
	mesh = all_positions[mesh_index, :]
	for h in range(mesh.shape[0]):
		# plot the underlying mesh for each section
		map_axes.plot(mesh[h, :, :, 0], mesh[h, :, :, 1], f"#bbb", linewidth=.3) # TODO: zoom in and stuff?
		map_axes.plot(mesh[h, :, :, 0].T, mesh[h, :, :, 1].T, f"#bbb", linewidth=.3)
		# project and plot the coastlines onto each section
		project = interpolate.RegularGridInterpolator([ф_mesh, λ_mesh], mesh[h, :, :, :],
		                                              bounds_error=False, fill_value=np.nan)
		for line in coastlines:
			projected_line = project(line)
			map_axes.plot(projected_line[:, 0], projected_line[:, 1], f"#000", linewidth=.8, zorder=2)
	if not final:
		# indicate the speed of each node
		map_axes.scatter(fit_positions[:, 0], fit_positions[:, 1], s=2,
		                 c=-np.linalg.norm(velocity, axis=1),
		                 cmap=CUSTOM_CMAP["speed"]) # TODO: zoom in and rotate automatically

	a, b = compute_principal_strains(all_positions, cell_definitions, cell_scales, dΦ, dΛ)

	# mark any nodes with nonpositive principal strains
	worst_cells = np.nonzero((a <= 0) | (b <= 0))[0]
	for cell in worst_cells:
		h, i, j, east, west, north, south = cell_definitions[cell, :]
		plt.plot(all_positions[[east, west], 0], all_positions[[east, west], 1], "#ff5f00")
		plt.plot(all_positions[[north, south], 0], all_positions[[north, south], 1], "#ff5f00")
	map_axes.axis("equal")

	# histogram the principal strains
	hist_axes.clear()
	hist_axes.hist2d(np.concatenate([a, b]),
	                 np.concatenate([b, a]),
	                 weights=np.tile(cell_weights, 2),
	                 bins=np.linspace(0, 2, 41),
	                 cmap=CUSTOM_CMAP["density"])
	hist_axes.axis("square")

	# plot the error function over time
	valu_axes.clear()
	valu_axes.plot(values)
	valu_axes.set_xlim(len(values) - 1000, len(values))
	valu_axes.set_ylim(0, 3*values[-1])
	valu_axes.minorticks_on()
	valu_axes.yaxis.set_tick_params(which='both')
	valu_axes.grid(which="both", axis="y")

	# plot the convergence criteria over time
	diff_axes.clear()
	diffs = -np.diff(values)/values[1:]
	if diffs.size > 0:
		diff_axes.scatter(np.arange(1, len(values)), diffs, s=1, zorder=10)
	diff_axes.scatter(np.arange(len(grads)), grads, s=1, zorder=10)
	ylim = max(2e-2, diffs.min(where=diffs != 0, initial=np.min(grads))*1e3)
	diff_axes.set_ylim(ylim/1e3, ylim)
	diff_axes.set_yscale("log")
	diff_axes.grid(which="major", axis="y")


def save_mesh(name: str, ф: np.ndarray, λ: np.ndarray, mesh: np.ndarray,
              section_borders: list[np.ndarray], section_names: list[str],
              descript: str) -> None:
	""" save all of the important map projection information as a HDF5 file.
	    :param name: the name of this elastick map projection
	    :param ф: the m latitude values at which the projection is defined
	    :param λ: the l longitude values at which the projection is defined
	    :param mesh: an n×m×l×2 array of the x and y coordinates at each point
	    :param section_borders: a list of the borders of the n sections. each one is an
	                            o×2 array of x and y coordinates, starting and ending at
	                            the same point.
	    :param section_names: a list of the names of the n sections. these will be added
	                          to the HDF5 file as attributes.
	    :param descript: a short description of the map projection, to be included in the
	                     HDF5 file as an attribute.
	"""
	assert len(section_borders) == len(section_names)

	with h5py.File(f"../projection/elastik-{name}.h5", "w") as file:
		file.attrs["name"] = name
		file.attrs["description"] = descript
		file.attrs["num_sections"] = len(section_borders)
		dset = file.create_dataset("bounding_box", shape=(2, 2))
		dset.attrs["units"] = "km"
		dset[:, :] = get_bounding_box(mesh)
		dset = file.create_dataset("sections", shape=(len(section_borders),), dtype=h5_str)
		dset[:] = [f"section{i}" for i in range(len(section_borders))]

		for h in range(len(section_borders)):
			i_relevant = dilate(np.any(~np.isnan(mesh[h, :, :, 0]), axis=1), 1)
			num_ф = np.sum(i_relevant)
			j_relevant = dilate(np.any(~np.isnan(mesh[h, 1:-1, :, 0]), axis=0), 1)
			num_λ = np.sum(j_relevant)

			group = file.create_group(f"section{h}")
			group.attrs["name"] = section_names[h]
			dset = group.create_dataset("latitude", shape=(num_ф,))
			dset.attrs["units"] = "°"
			dset[:] = np.degrees(ф[i_relevant])
			dset = group.create_dataset("longitude", shape=(num_λ,))
			dset.attrs["units"] = "°"
			dset[:] = np.degrees(λ[j_relevant])
			dset = group.create_dataset("projection", shape=(num_ф, num_λ, 2))
			dset.attrs["units"] = "km"
			dset[:, :, :] = mesh[h][i_relevant][:, j_relevant]
			dset = group.create_dataset("border", shape=section_borders[h].shape)
			dset.attrs["units"] = "°"
			dset[:, :] = np.degrees(section_borders[h])
			dset = group.create_dataset("bounding_box", shape=(2, 2))
			dset.attrs["units"] = "km"
			dset[:, :] = get_bounding_box(mesh[h, :, :, :])


def create_map_projection(configuration_file: str):
	""" create a map projection
	    :param configuration_file: "oceans" | "continents" | "countries"
	"""
	configure = load_options(configuration_file)
	print(f"loaded options from {configuration_file}")
	ф_mesh, λ_mesh, mesh, section_borders = load_mesh(configure["cuts"])
	print(f"loaded a {np.sum(np.isfinite(mesh[:, :, :, 0]))}-node mesh")
	scale = load_pixel_values(configure["scale"], configure["cuts"], mesh.shape[0], .03)
	print(f"loaded the {configure['scale']} map as the scale")
	weights = load_pixel_values(configure["weights"], configure["cuts"], mesh.shape[0], .03)
	print(f"loaded the {configure['weights']} map as the weights")
	width, height = (float(value) for value in configure["size"].split(","))
	print(f"setting the maximum map size to {width}×{height} km")

	# assume the coordinates are more or less evenly spaced
	dΦ = EARTH.a*(1 - EARTH.e2)*(1 - EARTH.e2*np.sin(ф_mesh)**2)**(3/2)*(ф_mesh[1] - ф_mesh[0])
	dΛ = EARTH.a*(1 + (1 - EARTH.e2)*np.tan(ф_mesh)**2)**(-1/2)*(λ_mesh[1] - λ_mesh[0])

	# reformat the nodes into a list without gaps or duplicates
	node_indices, node_positions = enumerate_nodes(mesh)

	# and then do the same thing for cell corners
	cell_definitions, cell_weights, cell_scales = enumerate_cells(node_indices, weights, scale, dΦ, dΛ)

	# define functions that can define the node positions from a reduced set of them
	transformations = []
	progression = np.ceil(np.geomspace(ф_mesh.size/10, 1.,
	                                   int(math.log2(ф_mesh.size/10)) + 1))
	for factor in progression:
		transformations.append(mesh_skeleton(node_indices, factor, ф_mesh))
	transformations.append((Scalar(1), Scalar(1))) # finishing with the full unreduced set

	# load the coastline data from Natural Earth
	coastlines = load_coastline_data()

	# set up the plotting axes
	small_fig = plt.figure(figsize=(3, 5), num=f"Elastik-{configuration_file} fitting")
	gridspecs = (plt.GridSpec(3, 1, height_ratios=[2, 1, 1]),
	             plt.GridSpec(3, 1, height_ratios=[2, 1, 1], hspace=0))
	hist_axes = small_fig.add_subplot(gridspecs[0][0, :])
	valu_axes = small_fig.add_subplot(gridspecs[1][1, :])
	diff_axes = small_fig.add_subplot(gridspecs[1][2, :], sharex=valu_axes)
	main_fig, map_axes = plt.subplots(figsize=(7, 5), num=f"Elastik-{configuration_file}")

	values, grads = [], []

	# define the objective functions
	def compute_energy_aggressive(positions: np.ndarray) -> float: # one that aggressively pushes the mesh to have all positive strains
		a, b = compute_principal_strains(restore @ positions,
		                                 cell_definitions, cell_scales, dΦ, dΛ)
		if np.all(a > 0) and np.all(b > 0):
			return -np.inf
		elif np.any(a < -100) or np.any(b < -100): # make this check to avoid annoying overflow warnings
			return np.inf
		else:
			a_term = np.exp(-6*a)
			b_term = np.exp(-6*b)
			return (a_term + b_term).sum()

	def compute_energy_lenient(positions: np.ndarray) -> float: # one that works in all domains
		a, b = compute_principal_strains(restore @ positions,
		                                 cell_definitions, cell_scales, dΦ, dΛ)
		if np.all(a > 0) and np.all(b > 0):
			return -np.inf
		else:
			scale_term = (a + b - 2)**2
			shape_term = (a - b)**2
			return ((scale_term + 2*shape_term)*cell_weights).sum()

	def compute_energy_strict(positions: np.ndarray) -> float: # and one that only works when all strains are positive
		a, b = compute_principal_strains(restore @ positions,
		                                 cell_definitions, cell_scales, dΦ, dΛ)
		if np.any(a <= 0) or np.any(b <= 0):
			return np.inf
		else:
			ab = a*b
			scale_term = (ab**2 - 1)/2 - np.log(ab)
			shape_term = (a - b)**2
			return ((scale_term + 2*shape_term)*cell_weights).sum()

	def plot_status(positions: np.ndarray, value: float, grad: np.ndarray, step: np.ndarray, final: bool) -> None:
		values.append(value)
		grads.append(np.linalg.norm(grad)*EARTH.R)
		if len(values) == 1 or np.random.random() < 1e-1 or final:
			all_positions = np.concatenate([restore @ positions, [[np.nan, np.nan]]])
			show_mesh(positions, all_positions, step, values, grads, final,
			          ф_mesh, λ_mesh, dΦ, dΛ, node_indices,
			          cell_definitions, cell_weights, cell_scales, coastlines,
			          map_axes, hist_axes, valu_axes, diff_axes)
			main_fig.canvas.draw()
			small_fig.canvas.draw()
			plt.pause(.01)

	# then minimize!
	print("begin fitting process")
	try:
		# progress from the coarsest transformd mesh to finer and finer ones
		for reduce, restore in transformations:
			node_positions = reduce @ node_positions
			node_positions = minimize(func=compute_energy_strict,
			                          backup_func=compute_energy_lenient,
			                          guess=node_positions,
			                          bounds=None,
			                          report=plot_status,
			                          tolerance=1e-3/EARTH.R)
			node_positions = restore @ node_positions

		# the mesh should, at some point, become well-behaved
		if not np.all(np.greater(compute_principal_strains(
			node_positions, cell_definitions, cell_scales, dΦ, dΛ), 0)):
			# if it doesn't do a final pass using the aggressive objective function to whip it into shape
			node_positions = minimize(func=compute_energy_strict,
			                          backup_func=compute_energy_aggressive,
			                          guess=node_positions,
			                          bounds=None,
			                          report=plot_status,
			                          tolerance=1e-3/EARTH.R)

	except RuntimeError as e:
		traceback.print_exc()
		small_fig.canvas.manager.set_window_title("Error!")
		plt.show()
		raise e

	# apply the optimized vector back to the mesh
	mesh = node_positions[node_indices, :]
	mesh[node_indices == -1, :] = np.nan

	# and save it!
	save_mesh(configure["name"], ф_mesh, λ_mesh, mesh,
	          section_borders, configure["section_names"].split(","),
	          configure["descript"])

	print(f"elastik {configure['name']} projection saved!")


if __name__ == "__main__":
	# create_map_projection("oceans")
	create_map_projection("continents")
	# create_map_projection("countries")

	plt.show()
