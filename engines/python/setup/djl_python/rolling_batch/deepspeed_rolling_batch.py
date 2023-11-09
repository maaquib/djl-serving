#!/usr/bin/env python
#
# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file
# except in compliance with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS"
# BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied. See the License for
# the specific language governing permissions and limitations under the License.

from djl_python.rolling_batch.rolling_batch import RollingBatch, stop_on_any_exception
from deepspeed.external.lmi_dist.utils.parameters import (
    NextTokenChooserParameters,
    StoppingCriteriaParameters,
)
from deepspeed.external.lmi_dist.utils.types import (Batch, Request)
from deepspeed.inference.engine import InferenceEngine
from deepspeed.inference.rolling_batch import DeepSpeedRollingBatchGeneration


class DeepSpeedRollingBatch(RollingBatch):

    def __init__(self, model: InferenceEngine, device, properties, **kwargs):
        """
        Initializes the LmiDistRollingBatch.

        :param model_id_or_path: model id or path
        :param device: model loaded device
        :param properties: other properties of the model, such as decoder strategy
        :param kwargs passed while loading the model
        """

        super().__init__(device, **kwargs)
        self.properties = properties
        self.batch_cls = None
        self.batch_id_counter = 0
        self.rolling_batch = DeepSpeedRollingBatchGeneration(
            model=model,
            tokenizer=kwargs.get("tokenizer"),
            max_batch_size=kwargs.get("max_batch_size"),
            max_seq_len=kwargs.get("max_seq_len"))

    def reset(self):
        self.rolling_batch.rolling_batch.clear()
        self.batch_id_counter = 0
        super().reset()

    def _warmup(self, **kwargs):
        pass

    @stop_on_any_exception
    def inference(self, input_data, parameters):
        """
        Performs prefill and decode operations for the batch.

        :param input_data: List of input texts for each request in a batch
        :param parameters: List of kwargs for each request in a batch
        :return: generated batch decoded tokens
        """
        new_requests = self.get_new_requests(input_data, parameters,
                                             len(input_data))
        new_batch = self.preprocess_requests(new_requests)
        if new_batch or len(self.active_requests) > 0:
            self._prefill_and_decode(new_batch)
        return self.postprocess_results()

    def _prefill_and_decode(self, new_batch):
        if new_batch:
            batch = new_batch
            generations, error_requests = self.rolling_batch.prefill_batch(
                batch)
            self.error_requests = error_requests
        else:
            generations = self.rolling_batch.generate_token()
        for request in self.active_requests:
            generation = None
            # TODO(mohaan): Change generations to a Dict with request id index
            filtered_gens = list(
                filter(lambda g: g.request_id == request.id, generations))
            if len(filtered_gens) > 0:
                generation = filtered_gens[0]
            if generation:
                is_last_token = generation.generated_text is not None

                request.set_next_token("" if generation.token_is_special else
                                       generation.token_text,
                                       self.output_formatter,
                                       last_token=is_last_token)
            else:
                request.set_next_token("",
                                       self.output_formatter,
                                       last_token=False)

    def preprocess_requests(self, requests, **kwargs):
        preprocessed_requests = []
        for r in requests:
            param = r.parameters
            parameters = NextTokenChooserParameters(
                temperature=param.get("temperature", 1.0),
                repetition_penalty=param.get("repetition_penalty", 1.0),
                top_k=param.get("top_k", 0),
                top_p=param.get("top_p", 1.0),
                typical_p=param.get("typical_p", 1.0),
                do_sample=param.get("do_sample", False),
                seed=int(param.get("seed", 0)))
            stop_parameters = StoppingCriteriaParameters(
                stop_sequences=param.get("stop_sequences", []),
                max_new_tokens=param.get("max_new_tokens", 30))

            request = Request(id=r.id,
                              inputs=r.input_text,
                              parameters=parameters,
                              stopping_parameters=stop_parameters)
            truncate = param.get("truncate", None)
            if truncate is not None:
                request.truncate = truncate
            preprocessed_requests.append(request)

        if preprocessed_requests:
            batch = Batch(id=self.batch_id_counter,
                          requests=preprocessed_requests,
                          size=len(preprocessed_requests))
            self.batch_id_counter += 1

            return batch
        else:
            return None
