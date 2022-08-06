#!/usr/bin/env python
"""
util.py

some handy utility functions that are used in multiple places
"""
import h5py
import numpy as np
from typing import Sequence


class Ellipsoid:
	def __init__(self, a, f):
		""" a collection of values defining a spheroid """
		self.a = a
		""" major semiaxis """
		self.f = f
		""" flattening """
		self.b = a*(1 - f)
		""" minor semiaxis """
		self.R = a
		""" equatorial radius """
		self.e2 = 1 - (self.b/self.a)**2
		""" square of eccentricity """
		self.e = np.sqrt(self.e2)
		""" eccentricity """

class Scalar:
	def __init__(self, value):
		""" a float with a matmul method """
		self.value = value

	def __matmul__(self, other):
		return self.value * other


h5_str = h5py.string_dtype(encoding='utf-8')
""" a str typ compatible with h5py """

EARTH = Ellipsoid(6_378.137, 1/298.257_223_563)
""" the figure of the earth as given by WGS 84 """


def bin_centers(bin_edges: np.ndarray) -> np.ndarray:
	""" calculate the center of each bin """
	return (bin_edges[1:] + bin_edges[:-1])/2

def bin_index(x: float | np.ndarray, bin_edges: np.ndarray) -> float | np.ndarray:
	""" I dislike the way numpy defines this function
	    :param: coerce whether to force everything into the bins (useful when there's roundoff)
	"""
	return np.where(x < bin_edges[-1], np.digitize(x, bin_edges) - 1, bin_edges.size - 2)

def index_grid(shape: Sequence[int]) -> Sequence[np.ndarray]:
	""" create a set of int matrices that together cover every index in an array of the given shape """
	indices = [np.arange(length) for length in shape]
	return np.meshgrid(*indices, indexing="ij")

def wrap_angle(x: float | np.ndarray) -> float | np.ndarray: # TODO: come up with a better name
	""" wrap an angular value into the range [-np.pi, np.pi) """
	return x - np.floor((x + np.pi)/(2*np.pi))*2*np.pi

def to_cartesian(ф: float | np.ndarray, λ: float | np.ndarray) -> tuple[float | np.ndarray, float | np.ndarray, float | np.ndarray]:
	""" convert spherical coordinates in degrees to unit-sphere cartesian coordinates """
	return (np.cos(np.radians(ф))*np.cos(np.radians(λ)),
	        np.cos(np.radians(ф))*np.sin(np.radians(λ)),
	        np.sin(np.radians(ф)))

def dilate(x: np.ndarray, distance: int) -> np.ndarray:
	""" take a 1D boolean array and make it so that any Falses near Trues become True.
	    then return the modified array.
	"""
	for i in range(distance):
		x[:-1] |= x[1:]
		x[1:] |= x[:-1]
	return x
