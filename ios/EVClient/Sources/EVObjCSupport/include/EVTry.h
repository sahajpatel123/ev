#import <AVFAudio/AVFAudio.h>
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

BOOL EVClientAudioAttachAndPrepare(AVAudioEngine *engine, AVAudioFormat * _Nullable * _Nullable outFormat, NSError * _Nullable * _Nullable outError);
BOOL EVClientAudioInstallTap(AVAudioInputNode *node, AVAudioFrameCount bufferSize, AVAudioFormat *format, AVAudioNodeTapBlock block, NSError * _Nullable * _Nullable outError);
BOOL EVClientAudioRemoveTap(AVAudioInputNode *node, NSError * _Nullable * _Nullable outError);
BOOL EVClientAudioStartEngine(AVAudioEngine *engine, NSError * _Nullable * _Nullable outError);
BOOL EVClientAudioStopEngine(AVAudioEngine *engine, NSError * _Nullable * _Nullable outError);

NS_ASSUME_NONNULL_END
