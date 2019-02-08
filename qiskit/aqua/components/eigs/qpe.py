# -*- coding: utf-8 -*-

# Copyright 2018 IBM.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================

import numpy as np
from qiskit import QuantumRegister
from qiskit.aqua import Operator, AquaError
from qiskit.aqua import Pluggable, PluggableType, get_pluggable_class
from qiskit.aqua.components.eigs import Eigenvalues
from qiskit.aqua.algorithms.single_sample import PhaseEstimation


class QPE(Eigenvalues):
    """A QPE for getting the eigenvalues."""

    CONFIGURATION = {
        'name': 'QPE',
        'description': 'Quantum Phase Estimation',
        'input_schema': {
            '$schema': 'http://json-schema.org/schema#',
            'id': 'qpe_schema',
            'type': 'object',
            'properties': {
                'num_time_slices': {
                    'type': 'integer',
                    'default': 1,
                    'minimum': 0
                },
                'expansion_mode': {
                    'type': 'string',
                    'default': 'trotter',
                    'oneOf': [
                        {'enum': [
                            'suzuki',
                            'trotter'
                        ]}
                    ]
                },
                'expansion_order': {
                    'type': 'integer',
                    'default': 1,
                    'minimum': 1
                },
                'num_ancillae': {
                    'type': 'integer',
                    'default': 1,
                    'minimum': 1
                },
                'evo_time': {
                    'type': ['number', 'null'],
                    'default': None
                },
                'use_basis_gates': {
                    'type': 'boolean',
                    'default': True,
                },
                'hermitian_matrix': {
                    'type': 'boolean',
                    'default': True
                },
                'negative_evals': {
                    'type': 'boolean',
                    'default': False
                },
            },
            'additionalProperties': False
        },
        'depends': [
            {'pluggable_type': 'iqft',
             'default': {
                     'name': 'STANDARD',
                }
             },
            {'pluggable_type': 'qft',
             'default': {
                     'name': 'STANDARD',
                }
             },
        ],
    }

    def __init__(self, operator, iqft,
                 num_time_slices=1, num_ancillae=1,
                 expansion_mode="trotter",
                 expansion_order=1, evo_time=None,
                 use_basis_gates=True, hermitian_matrix=True,
                 negative_evals=False, ne_qfts=[None, None]):

        super().__init__()
        super().validate(locals())
        self._num_ancillae = num_ancillae
        self._iqft = iqft
        self._operator = operator
        self._num_time_slices = num_time_slices
        self._expansion_mode = expansion_mode
        self._expansion_order = expansion_order
        self._evo_time = evo_time
        self._use_basis_gates = use_basis_gates
        self._hermitian_matrix = hermitian_matrix
        self._negative_evals = negative_evals
        self._ne_qfts = ne_qfts
        self._init_constants()
        self._ret = {}

    @classmethod
    def init_params(cls, params, matrix):
        """
        Initialize via parameters dictionary and algorithm input instance
        Args:
            params: parameters dictionary
            matrix: two dimensional array which represents the operator
        """
        if matrix is None:
            raise AquaError("Operator instance is required.")

        if not isinstance(matrix, np.ndarray):
            matrix = np.array(matrix)

        eigs_params = params.get(Pluggable.SECTION_KEY_EIGS)
        args = {k: v for k, v in eigs_params.items() if k != 'name'}
        num_ancillae = eigs_params['num_ancillae']
        hermitian_matrix = eigs_params['hermitian_matrix']
        negative_evals = eigs_params['negative_evals']

        # Adding an automatic flag qubit for negative eigenvalues
        if negative_evals:
            num_ancillae += 1
            args['num_ancillae'] = num_ancillae

        # If operator matrix is not hermitian, extending it to B = ((0, A), (A⁺, 0)), which is hermitian
        # In this case QPE will give singular values
        if not hermitian_matrix:
            negative_evals = True
            new_matrix = np.zeros((2*matrix.shape[0], 2*matrix.shape[0]), dtype=complex)
            new_matrix[matrix.shape[0]:,:matrix.shape[0]] = np.matrix.getH(matrix)[:,:]
            new_matrix[:matrix.shape[0],matrix.shape[0]:] = matrix[:,:]
            matrix = new_matrix
        args['operator'] = Operator(matrix=matrix)

        # Set up iqft, we need to add num qubits to params which is our num_ancillae bits here
        iqft_params = params.get(Pluggable.SECTION_KEY_IQFT)
        iqft_params['num_qubits'] = eigs_params['num_ancillae']
        args['iqft'] = get_pluggable_class(PluggableType.IQFT,
                                           iqft_params['name']).init_params(params)

        # For converting the encoding of the negative eigenvalues, we need two
        # additional QFTs
        if negative_evals:
            ne_params = params
            qft_num_qubits = iqft_params['num_qubits']
            ne_qft_params = params.get(Pluggable.SECTION_KEY_QFT)
            ne_qft_params['num_qubits'] = qft_num_qubits - 1
            ne_iqft_params = params.get(Pluggable.SECTION_KEY_IQFT)
            ne_iqft_params['num_qubits'] = qft_num_qubits - 1
            ne_params['qft'] = ne_qft_params
            ne_params['iqft'] = ne_iqft_params
            args['ne_qfts'] = [get_pluggable_class(PluggableType.QFT,
                                                   ne_qft_params['name']).init_params(ne_params),
                               get_pluggable_class(PluggableType.IQFT,
                                                   ne_iqft_params['name']).init_params(ne_params)]
        else:
            args['ne_qfts'] = [None, None]

        return cls(**args)

    def _init_constants(self):
        # estimate evolution time
        self._operator._check_representation('paulis')
        paulis = self._operator.paulis
        if self._evo_time == None:
            lmax = sum([abs(p[0]) for p in self._operator.paulis])
            if not self._negative_evals:
                self._evo_time = (1-2**-self._num_ancillae)*2*np.pi/lmax
            else:
                self._evo_time = (1/2-2**-self._num_ancillae)*2*np.pi/lmax

        # check for identify paulis to get its coef for applying global phase shift on ancillae later
        num_identities = 0
        for p in self._operator.paulis:
            if np.all(p[1].z == 0) and np.all(p[1].x == 0):
                num_identities += 1
                if num_identities > 1:
                    raise RuntimeError('Multiple identity pauli terms are present.')
                self._ancilla_phase_coef = p[0].real if isinstance(p[0], complex) else p[0]

    def get_register_sizes(self):
        return self._operator.num_qubits, self._num_ancillae

    def get_scaling(self):
        return self._evo_time

    def construct_circuit(self, mode, register=None):
        """Implement the Quantum Phase Estimation algorithm"""

        pe = PhaseEstimation(operator=self._operator,
                             state_in=None, iqft=self._iqft,
                             num_time_slices=self._num_time_slices,
                             num_ancillae=self._num_ancillae,
                             expansion_mode=self._expansion_mode,
                             expansion_order=self._expansion_order,
                             evo_time=self._evo_time)

        if mode == 'vector':
            raise ValueError("QPE only posslible as circuit not vector.")

        a = QuantumRegister(self._num_ancillae)
        q = register

        qc = pe.construct_circuit(state_register=q, ancillary_register=a)

        # handle negative eigenvalues
        if self._negative_evals:
            self._handle_negative_evals(qc, a)

        self._circuit = qc
        self._output_register = a
        self._input_register = q
        return self._circuit

    def _handle_negative_evals(self, qc, q):
        sgn = q[0]
        qs = [q[i] for i in range(1, len(q))]
        for qi in qs:
            qc.cx(sgn, qi)
        self._ne_qfts[0].construct_circuit('circuit', qs, qc)
        for i, qi in enumerate(reversed(qs)):
            qc.cu1(2*np.pi/2**(i+1), sgn, qi)
        self._ne_qfts[1].construct_circuit('circuit', qs, qc)
