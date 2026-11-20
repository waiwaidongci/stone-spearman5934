from pydantic import BaseModel,Field
class ExclusionPreviewInput(BaseModel):
    study_id:int=Field(gt=0)
    forced_keys:list[str]=Field(default_factory=list,max_length=5000)
class ExclusionTrialInput(ExclusionPreviewInput):
    name:str=Field(min_length=2,max_length=120)
    basis_checksum:str=Field(min_length=64,max_length=64)
